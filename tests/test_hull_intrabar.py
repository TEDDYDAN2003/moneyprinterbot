"""Live-continuation contract. Synthetic native MT5 rates, ticks and fake orders ONLY."""

import json
import pickle
import sys
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_cases import PATH
from hull_mt import MT, ROOT, T, scanner_clock, sig

from bot.config.loader import load_yaml
from bot.hull.config import LIVE_CONTINUATION, HullConfig, Indicator, selection
from bot.hull.entry_clock import LIVE_QUOTE_POLICY
from bot.hull.live import LiveSampler
from bot.hull.orders import Block, Grid, make_plan
from bot.hull.signal import Hull, LiveTick
from bot.hull.terminal import Stream, dispatch, run
from bot.zones.engine import Bar

NAME = "XAUUSDm"
OK = dict(data_ok=True, history_ok=True, rewarmed=False)
FIX = json.loads((ROOT / "MT5/qa/protected_cases.json").read_text())["fixtures"]


def bars(d, tf=1):
    rows = FIX["protected-buy" if d == 1 else "protected-sell"]
    return [Bar(rows[0][0] + i * tf * 60, *r[1:]) for i, r in enumerate(rows)]


def rate(b):
    return dict(time=b.t, open=b.o, high=b.h, low=b.l, close=b.c)


def to_now(b, prices):
    return Bar(b.t, b.o, max(b.o, *prices), min(b.o, *prices), prices[-1])


def quote(b, ms):
    return LiveTick(b.t * 1000 + ms, b.c, round(b.c + 0.02, 3))


@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("tf", [1, 2, 5])
@pytest.mark.parametrize("stop", ["local_pullback", "confirmed_pivot"])
def test_forming_projection_uses_only_closed_structure_stop_and_atr(
    direction, tf, stop
):
    tape = bars(direction, tf)
    p = Indicator(stop=stop)
    h, confirmed = Hull(tf, p, 0.01), Hull(tf, p, 0.01)
    for b in tape[:92]:
        last = h.update(b)
        confirmed.update(b)
    before = pickle.dumps(h)
    b = tape[92]
    prices = []
    for i, px in enumerate([b.o, b.c, b.o, b.c, b.o, b.c]):
        prices.append(px)
        c = to_now(b, prices)
        r = h.preview(c, quote(c, 250 + i * 500))
        assert r.label == (direction if i % 2 else 0)
        assert r.structure == last.structure and r.atr == last.atr
        if r.label:
            assert r.close_s > r.signal_s
            if stop == "local_pullback":
                assert r.entry_side == direction
            else:
                # Last CONFIRMED pivot is on the wrong side here. Do not invent a new one.
                assert (
                    not r.entry_side
                    and r.entry_reason == "swing_already_lost_at_trigger"
                )
            assert r.anchor_s <= h.prev.t and r.structure.pullback_seen_s <= b.t
            if stop == "confirmed_pivot":
                assert r.anchor == (h.low if direction == 1 else h.high).price
            assert r.raw_sl == r.anchor - direction * p.atr_buffer * last.atr
        assert pickle.dumps(h) == before
    # A wick through the REAL guard cannot reverse the closed state or promote a swing.
    g = last.structure.origin.price
    through = replace(b, h=max(b.h, g + 1), l=min(b.l, g - 1), c=g - direction * 0.5)
    assert h.preview(through, quote(through, 8000)).entry_side == 0
    assert pickle.dumps(h) == before
    assert [h.update(c) for c in tape[92:]] == [confirmed.update(c) for c in tape[92:]]


@pytest.mark.parametrize("direction", [-1, 1])
def test_intrabar_cross_cannot_supply_its_own_first_completed_pullback(direction):
    h = Hull(1, Indicator(hull_len=2, source="close", swing_len=1, atr_length=2), 0.01)
    for i, v in enumerate(PATH[:10]):
        x = v if direction == 1 else 240 - v
        b = Bar(T + 60 * i, x, x + 0.25, x - 0.25, x)
        if i == 9:
            assert (
                not h.structure.view.continuation_side
                and h.structure.view.phase == "wait_pullback"
            )
            r = h.preview(b, quote(b, 16000))
            assert (
                r.side == direction
                and not r.entry_side
                and r.entry_reason == "live_requires_completed_pullback"
            )
        closed = h.update(b)
    assert (
        closed.structure.pullback_seen_s == closed.close_s
        and closed.entry_side == direction
    )


def test_each_projected_raw_source_matches_if_committed_once_including_conflict_and_agreement():
    from test_hull_either_v2 import tape

    h = Hull(1, Indicator(), 0.01)
    marks = set()
    for i, b in enumerate(tape(1)):
        if i:
            tentative = h.preview(b, quote(b, 250))
        closed = h.update(b)
        if i:
            assert (
                tentative.a_low,
                tentative.a_high,
                tentative.b,
                tentative.cross_low,
                tentative.cross_high,
                tentative.label,
            ) == (
                closed.a_low,
                closed.a_high,
                closed.b,
                closed.cross_low,
                closed.cross_high,
                closed.label,
            )
            marks.add((tentative.cross_low, tentative.cross_high))
            if tentative.source_conflict:
                assert (
                    not tentative.entry_side
                    and tentative.entry_reason == "source_conflict"
                )
    assert {(1, 1), (-1, -1)} <= marks and ((1, -1) in marks or (-1, 1) in marks)


def simulate(tmp_path, monkeypatch, d, case):
    """Real scanner loop with finite virtual ticks. No MetaTrader5 package/host."""
    reverse = case.startswith("reverse")
    idx = 45 if reverse else 92
    data = bars(d)
    b = data[idx]
    mt = MT()
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["run"]["mode"] = "demo"
    cfg["hull"]["symbols"]["XAUUSD"]["session"] = "continuous"
    if case == "last_chart":
        mt.i.chart_mode = 1
    h = HullConfig.model_validate(cfg["hull"])
    core = Hull(1, h.indicator, 0.01)
    for closed in data[:idx]:
        core.update(closed)
    assert core.structure.view.continuation_side == (-d if reverse else d)
    # Bid ticks reach the crossover at exactly :02.360, fall back, then either
    # close on the cross or vanish. Never hand the scanner future high/low.
    schedule = [(0.070, b.o), (2.360, b.c), (2.590, b.o), (3.080, b.c)]
    final = b.o if case in ("vanish", "reverse_wick") else b.c
    schedule.append((59.950, final))
    polls = (
        [-0.25, 0.080, 2.375, 2.625, 3.125, 60.175]
        if case != "attach"
        else [2.375, 2.625, 3.125, 60.175]
    )
    phase = [0]

    def pos():
        return polls[phase[0]]

    def wall():
        return b.t + pos()

    def forming():
        if pos() < 0:
            return data[idx - 1], b.t * 1000 - 300
        if pos() >= 60:
            return Bar(b.t + 60, final, final, final, final), (b.t + 60) * 1000 + 120
        points = [(delta, px) for delta, px in schedule if pos() >= delta]
        return to_now(b, [px for _, px in points]), b.t * 1000 + round(
            points[-1][0] * 1000
        )

    def tick(_):
        current, ms = forming()
        spread = 2.0 if case == "spread" and pos() == 2.375 else 0.02
        return NS(time_msc=ms, bid=current.c, ask=round(current.c + spread, 3))

    def rates(_, tf, shift, count):
        assert tf == 1
        if shift == 0:
            assert count == 1
            return [rate(forming()[0])]
        assert shift == 1
        n = idx - int(pos() < 0)
        out = data[:n]
        if pos() >= 60:
            out += [to_now(b, [px for _, px in schedule])]
        return [rate(x) for x in out[-count:]]

    when_sent = []
    send = mt.order_send

    def record(req):
        when_sent.append(pos())
        return send(req)

    if reverse:
        mt.positions.append(
            NS(
                ticket=941,
                symbol=NAME,
                type=mt.ORDER_TYPE_SELL if d == 1 else mt.ORDER_TYPE_BUY,
                magic=h.magic,
                volume=0.01,
                price_open=b.o,
                sl=b.o + 10 * d,
                tp=b.o - 10 * d,
                profit=0,
            )
        )
    mt.copy_rates_from_pos, mt.symbol_info_tick, mt.order_send = rates, tick, record
    mt.initialize, mt.shutdown = lambda: True, lambda: None

    def advance(seconds):
        assert (
            0 < seconds <= 0.25 + 1e-6
        )  # even with --poll-seconds 10, when already watching
        phase[0] += 1
        if phase[0] == 3 and not reverse:
            mt.positions.clear()  # already flat when it re-crosses and later confirms

    scanner_clock(monkeypatch, wall, advance, lambda: 500.0 + pos())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt)
    run(
        cfg,
        h,
        selection(h, ["XAUUSD"], rate="medium"),
        (1,),
        armed=True,
        poll=10,
        iterations=len(polls),
    )
    ev = [
        json.loads(line)
        for line in next(tmp_path.glob("reports/hull_runtime/*/demo.jsonl"))
        .read_text()
        .splitlines()
    ]
    assert ev[0]["summary"]["entry_timing"] == LIVE_CONTINUATION
    assert ev[0]["summary"]["entry_quote_policy"] == LIVE_QUOTE_POLICY
    return b, mt, when_sent, ev


@pytest.mark.parametrize("d", [-1, 1])
@pytest.mark.parametrize(
    "case", ["confirm", "vanish", "spread", "attach", "last_chart"]
)
def test_first_executable_live_continuation_through_actual_run_loop(
    tmp_path, monkeypatch, d, case
):
    b, mt, sent, ev = simulate(tmp_path, monkeypatch, d, case)
    triggers = [v for v in ev if v["event"] == "live_hull_cross"]
    closed = [
        v for v in ev if v["event"] == "closed_label" and v["close_s"] == b.t + 60
    ]
    decisions = [v["outcome"] for v in ev if v["event"] == "label_decision"]
    if case == "vanish":
        assert not closed  # deal happened but the chart never confirms a label
    else:
        assert (
            len(closed) == 1
            and closed[0]["confirmation_only"]
            and closed[0]["entry_reason"] == "continuation_live_only"
        )
    if case in ("attach", "last_chart"):
        assert not triggers and not sent
        return
    (r,) = triggers
    ms = b.t * 1000 + 2360
    assert r["trigger_msc"] == ms and r["entry_timing"] == "live_hull_cross"
    assert (
        r["signal_close_utc"] is None
        and r["bar_end_utc"] > r["trigger_utc"] > r["bar_open_utc"]
    )
    assert (
        r["detected_lag_ms"] == 15
        and r["entry_side"] == d
        and r["pullback_seen_close_s"] <= b.t
    )
    if case == "spread":
        assert not sent and "spread_cap" in decisions
    else:
        assert sent == [2.375] * 4 and "demo_batch_sent" in decisions
        for leg, req in enumerate(mt.requests, 1):
            assert req["comment"] == f"i1_{ms}_{leg}"
            assert req["price"] == (
                r["trigger"]["ask"] if d == 1 else r["trigger"]["bid"]
            )
            assert (
                d * (req["price"] - req["sl"]) > 0
                and d * (req["tp"] - req["price"]) > 0
            )
    assert not [v for v in ev if v["event"] in ("exit_request", "opposite_label_exit")]


@pytest.mark.parametrize("d", [-1, 1])
@pytest.mark.parametrize("case", ["reverse_confirm", "reverse_wick"])
def test_reversal_needs_real_closing_break_and_opposite_exit_is_not_provisional(
    tmp_path, monkeypatch, d, case
):
    b, mt, sent, ev = simulate(tmp_path, monkeypatch, d, case)
    assert not [v for v in ev if v["event"] == "live_hull_cross"]
    if case == "reverse_wick":
        assert not sent and mt.positions[0].ticket == 941
    else:
        (r,) = [
            v for v in ev if v["event"] == "closed_label" and v["close_s"] == b.t + 60
        ]
        assert r["structure"]["event"] == "choch" and r["structure"]["break_now"]
        assert r["entry_side"] == d and r["signal_close_utc"] == r["trigger_utc"]
        assert sent == [60.175] * 5 and mt.requests[0]["position"] == 941
        assert all(q["comment"].startswith(f"h1_{b.t + 60}_") for q in mt.requests[1:])
        assert len([v for v in ev if v["event"] == "exit_request"]) == 1


def live_sig(d=1, tf=1, start=T, offset=100):
    tick = LiveTick(start * 1000 + offset, 2000.0, 2000.1)
    return replace(
        sig(side=d, sl=1999.95 if d == 1 else 2000.55),
        tf=tf,
        close_s=start + tf * 60,
        trigger=tick,
    )


@pytest.mark.parametrize(
    "change,why",
    [
        ("stale", "quote_too_old"),
        ("old", "quote_precedes_live_trigger"),
        ("future", "quote_clock_ahead"),
        ("moved", "live_trigger_quote_changed"),
        ("stop", "swing_breached_or_too_close"),
        ("exposure", "portfolio_lot_limit"),
        ("funding", "insufficient_free_margin"),
        ("pending", "pending_orders_present"),
        ("ended", "live_signal_bar_ended"),
        ("kill", "kill"),
    ],
)
def test_live_execution_rejects_unsafe_signal_without_wait_or_retry(
    hull_desk, monkeypatch, change, why
):
    mt, d, _ = hull_desk()
    r = live_sig()
    if change == "stale":
        monkeypatch.setattr("bot.hull.desk.time.time", lambda: T + 5.0)
    elif change == "old":
        mt.q.time_msc -= 1
    elif change == "future":
        mt.q.time_msc += 500
    elif change == "moved":
        mt.q.bid += 0.01
    elif change == "stop":
        r = replace(r, raw_sl=mt.q.bid)
    elif change == "exposure":
        d.lim = d.lim.model_copy(update={"max_total_lots": 0.01})
    elif change == "funding":
        mt.a.margin_free = 0.01
    elif change == "pending":
        mt.orders.append(9)
    elif change == "ended":
        r = live_sig(offset=59900)
        mt.q.time_msc = (T + 60) * 1000
        monkeypatch.setattr("bot.hull.desk.time.time", lambda: T + 60.0)
    else:
        d.kill.touch()
    assert d.batch(NAME, r) == why
    assert not mt.requests and not d.waiting and not d.state.get("manual_hold")
    assert d.batch(NAME, r) == "already_consumed"


@pytest.mark.parametrize("side", [-1, 1])
def test_same_quote_many_timeframes_one_batch_persisted_lanes_not_future_watermark(
    hull_desk, monkeypatch, side
):
    mt, desk, log = hull_desk("medium")
    labels = [live_sig(side, tf=tf) for tf in (2, 1, 5)]
    dispatch(desk, NAME, labels, **OK)
    assert len(mt.requests) == 4 and all(
        q["comment"].startswith("i5_") for q in mt.requests
    )
    assert log[-1][1]["tf"] == 5 and not desk.waiting
    mt.positions.clear()
    state = json.loads(desk.path.read_text())
    _, restart, _ = hull_desk("medium", state=state, mt=mt)
    for r in labels:
        assert (
            restart.batch(
                NAME, replace(r, trigger=replace(r.trigger, ms=r.trigger.ms + 200))
            )
            == "already_consumed"
        )
        assert restart.batch(NAME, replace(r, trigger=None)) == "already_consumed"
    # A DIFFERENT fresh M1 bar can qualify after flat, before the original M5 candle ends.
    fresh = live_sig(side, start=T + 60)
    mt.q.time_msc = fresh.trigger.ms
    monkeypatch.setattr("bot.hull.desk.time.time", lambda: T + 60.2)
    assert restart.batch(NAME, fresh) == "demo_batch_sent" and len(mt.requests) == 8


def test_conflicting_live_lanes_skip_without_any_exit(hull_desk, monkeypatch):
    mt, desk, log = hull_desk()
    monkeypatch.setattr(
        desk, "close_opposite", lambda *_: pytest.fail("provisional exit")
    )
    dispatch(desk, NAME, [live_sig(1), live_sig(-1, tf=2)], **OK)
    assert not mt.requests and log[-1][1]["outcome"] == "opposite_labels_same_trigger"


def test_both_observed_bid_and_executable_quote_obey_strict_protected_edge(hull_desk):
    mt, desk, _ = hull_desk()
    g = Grid.from_info(mt.i)
    for side in (-1, 1):
        r = live_sig(side)
        edge = r.structure.origin.price
        assert not replace(r, close=edge).entry_side
        with pytest.raises(Block, match="quote_outside_"):
            make_plan(r, desk.syms[NAME][1], g, edge, edge, desk.cash(NAME))


@pytest.mark.parametrize(
    "offset,expected",
    [
        (-1, "shadow_plan_only"),
        (0, "same_side_batch_at_live_trigger"),
        (1, "same_side_batch_at_live_trigger"),
    ],
)
def test_millisecond_flat_at_live_trigger_not_future_candle_close(
    hull_desk, offset, expected
):
    mt, desk, _ = hull_desk(armed=False)
    r = live_sig()
    mt.deals = [
        NS(
            symbol=NAME,
            entry=mt.DEAL_ENTRY_OUT,
            type=mt.DEAL_TYPE_SELL,
            time_msc=r.trigger.ms + offset,
        )
    ]
    assert desk.batch(NAME, r) == expected


def test_two_snapshot_race_disconnect_and_restart_do_not_replay_an_active_cross():
    tape = bars(1)
    lane = Stream(1, Indicator(), 0.01, live=True)
    for x in tape[:92]:
        lane.h.update(x)
    b = tape[92]
    logs = []
    sampler = LiveSampler({1: lane}, lambda *a, **k: logs.append((a, k)))
    sampler.reset(b.t - 0.5)
    mt = MT()
    c = to_now(b, [b.c])
    tick = quote(c, 100)
    q = NS(time_msc=tick.ms, bid=tick.bid, ask=tick.ask)
    mt.copy_rates_from_pos = lambda *a: [rate(c)]
    seq = iter([q, NS(time_msc=tick.ms + 1, bid=tick.bid, ask=tick.ask)])
    mt.symbol_info_tick = lambda _: next(seq)
    assert not sampler.sample(mt, NAME, lambda: b.t + 0.2, 4)
    assert not sampler.used
    mt.symbol_info_tick = lambda _: None
    assert not sampler.sample(mt, NAME, lambda: b.t + 0.25, 4) and sampler.disconnected
    mt.symbol_info_tick = lambda _: q
    assert not sampler.sample(mt, NAME, lambda: b.t + 0.3, 4)
    assert not LiveSampler({1: lane}, sampler.emit).sample(
        mt, NAME, lambda: b.t + 0.4, 4
    )
    assert not sampler.used and lane.h.prev == tape[91]


@pytest.mark.parametrize(
    "phase,expected,sent",
    [
        ("first_moved", "demo_batch_sent", 4),
        ("first_gone", "live_cross_no_longer_verified", 0),
        ("final_gone", "live_cross_no_longer_verified", 0),
        (
            "next_leg_gone",
            "protected_partial_no_topup:live_cross_no_longer_verified",
            1,
        ),
        ("next_leg_moved", "demo_batch_sent", 4),
    ],
)
def test_changed_quotes_revalidate_actual_forming_hull_at_every_stage(
    hull_desk, monkeypatch, phase, expected, sent
):
    mt, d, _ = hull_desk("medium")
    d.syms[NAME] = (
        d.syms[NAME][0],
        d.syms[NAME][1].model_copy(update={"session": "continuous"}),
    )
    lane = Stream(1, Indicator(), 0.01, live=True)
    data = bars(1)
    for x in data[:92]:
        lane.h.update(x)
    sampler = LiveSampler({1: lane}, lambda *a, **kw: None)
    b = data[92]
    current = [to_now(b, [b.c])]
    original = quote(current[0], 100)
    r = lane.h.preview(current[0], original)
    call = [0]

    def tick(_):
        call[0] += 1
        # Still-cross verification also brackets the bar read with a tick. Once the market
        # moves here, all further quotes are stable (including the re-verification read).
        key = 1 if phase.startswith("first") else 3 if phase.startswith("final") else 4
        px = b.c if call[0] < key else b.o if phase.endswith("gone") else b.h
        current[0] = to_now(b, [b.c, px])
        t = quote(current[0], 100 if call[0] < key else 200)
        return NS(time_msc=t.ms, bid=t.bid, ask=t.ask)

    monkeypatch.setattr("bot.hull.desk.time.time", lambda: b.t + 0.3)
    mt.symbol_info_tick = tick
    mt.copy_rates_from_pos = lambda *args: [rate(current[0])]
    d.live_check = lambda name, r, q: sampler.still_crossed(mt, name, r, q)
    assert d.batch(NAME, r) == expected and len(mt.requests) == sent
    assert not d.state.get("manual_hold") and not d.waiting
    if phase == "first_moved":
        assert all(req["price"] == round(b.h + 0.02, 3) for req in mt.requests)
    if phase == "next_leg_moved":
        assert [req["price"] for req in mt.requests] == [original.ask] + [
            round(b.h + 0.02, 3)
        ] * 3
    assert d.batch(NAME, r) == "already_consumed"


def test_wide_trigger_spread_is_not_rescued_by_narrower_quote_during_checks(hull_desk):
    mt, d, _ = hull_desk()
    r = live_sig()
    r = replace(r, trigger=replace(r.trigger, ask=2005.0))
    d.live_check = lambda *_: True
    assert d.batch(NAME, r) == "spread_cap" and not mt.requests


@pytest.mark.parametrize("direction", [-1, 1])
def test_live_continuation_never_closes_an_opposite_ticket_to_make_room(
    hull_desk, direction
):
    mt, d, logs = hull_desk("medium")
    r = live_sig(direction)
    mt.positions.append(
        NS(
            symbol=NAME,
            magic=d.h.magic,
            ticket=52,
            type=d.typ(-direction),
            sl=2000 + direction,
            tp=2000 - direction,
            price_open=2000.0,
            volume=0.01,
        )
    )
    dispatch(d, NAME, [r], **OK)
    assert not mt.requests and mt.positions[0].ticket == 52
    assert logs[-1][1]["outcome"] == "symbol_busy_or_batch_changed"


def test_live_candidate_supersedes_pending_close_without_replaying_its_exit(
    hull_desk, monkeypatch
):
    mt, d, logs = hull_desk(armed=False)
    q = mt.q
    mt.q = None
    exits = []
    monkeypatch.setattr(
        d, "close_opposite", lambda name, r: exits.append(r.signal_msc) or 0
    )
    dispatch(d, NAME, [sig()], **OK)
    assert NAME in d.waiting
    mt.q = q
    dispatch(d, NAME, [live_sig(tf=5)], **OK)
    assert exits == [T * 1000] and not d.waiting and not mt.requests
    assert [
        (v["entry_timing"], v["outcome"])
        for event, v in logs
        if event == "label_decision"
    ] == [
        ("confirmed_close", "first_quote_wait_superseded"),
        ("live_hull_cross", "shadow_plan_only"),
    ]


def test_closed_label_benchmark_rejects_live_inputs_instead_of_backdating():
    from bot.hull.replay import Account

    with pytest.raises(ValueError, match="Closed-label benchmark"):
        Account.step(None, None, [live_sig()])
