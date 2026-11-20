"""Closed-only v4 contract, including attempts to feed the retired live clock. Synthetic native MT5 rates, ticks and fake orders ONLY."""

import json
import pickle
import sys
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_cases import PATH
from hull_mt import MT, ROOT, T, scanner_clock, sig

from bot.config.loader import load_yaml
from bot.hull.config import (
    CLOSE_ONLY,
    RETIRED_LIVE_TIMING,
    HullConfig,
    Indicator,
    selection,
)
from bot.hull.entry_clock import QUOTE_POLICY
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
            assert not r.entry_side and r.entry_reason == "closed_cross_required"
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
                    and tentative.entry_reason == "closed_cross_required"
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
    if case == "legacy_timing":
        cfg["hull"]["entry_timing"] = RETIRED_LIVE_TIMING
    h = HullConfig.model_validate(cfg["hull"])
    core = Hull(1, h.indicator, 0.01)
    for closed in data[:idx]:
        core.update(closed)
    assert core.structure.view.bias == (-d if reverse else d)
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
    if case == "historical":
        polls = [60.175]
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
        spread = 2.0 if case == "spread" and pos() >= 60 else 0.02
        return NS(time_msc=ms, bid=current.c, ask=round(current.c + spread, 3))

    def rates(_, tf, shift, count):
        assert tf == 1
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
        assert 0 < seconds <= 10
        phase[0] += 1

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
    assert ev[0]["summary"]["entry_timing"] == CLOSE_ONLY
    assert ev[0]["summary"]["entry_quote_policy"] == QUOTE_POLICY
    assert ev[0]["summary"]["retired_clock_migrated"] == (case == "legacy_timing")
    return b, mt, when_sent, ev


@pytest.mark.parametrize("d", [-1, 1])
@pytest.mark.parametrize(
    "case", ["confirm", "vanish", "spread", "attach", "legacy_timing", "historical"]
)
def test_forming_cross_cannot_trade_and_only_a_new_closed_trend_cross_can(
    tmp_path, monkeypatch, d, case
):
    b, mt, sent, events = simulate(tmp_path, monkeypatch, d, case)
    assert not [v for v in events if v["event"] == "live_hull_cross"]
    closed = [
        v for v in events if v["event"] == "closed_label" and v["close_s"] == b.t + 60
    ]
    if case in ("vanish", "historical"):
        assert not closed and not sent and not mt.requests
        return
    assert (
        len(closed) == 1
        and closed[0]["entry_side"] == d
        and closed[0]["entry_timing"] == "confirmed_close"
    )
    assert closed[0]["entry_setup"] == (
        "bull_bos_discount_buy" if d == 1 else "bear_bos_premium_sell"
    )
    if case == "spread":
        assert not sent and any(v.get("outcome") == "spread_cap" for v in events)
    else:
        assert sent == [60.175] * 4 and all(
            q["comment"].startswith(f"h1_{b.t + 60}_") for q in mt.requests
        )
        assert all(
            q["action"] == mt.TRADE_ACTION_DEAL and q["sl"] > 0 and q["tp"] > 0
            for q in mt.requests
        )


@pytest.mark.parametrize("d", [-1, 1])
@pytest.mark.parametrize("case", ["reverse_confirm", "reverse_wick"])
def test_true_choch_and_wick_cannot_open_but_closed_opposite_still_exits(
    tmp_path, monkeypatch, d, case
):
    b, mt, sent, events = simulate(tmp_path, monkeypatch, d, case)
    if case == "reverse_wick":
        assert not sent and mt.positions[0].ticket == 941
    else:
        r = next(
            v
            for v in events
            if v["event"] == "closed_label" and v["close_s"] == b.t + 60
        )
        assert (
            r["entry_side"] == 0
            and r["entry_reason"] == "structure_choch_context_only"
            and r["side"] == d
        )
        assert (
            sent == [60.175]
            and len(mt.requests) == 1
            and mt.requests[0]["position"] == 941
        )
    assert not any(v["event"] in ("batch_preview", "live_hull_cross") for v in events)


def live_sig(d=1, tf=1, start=T, offset=100):
    tick = LiveTick(start * 1000 + offset, 2000.0, 2000.1)
    return replace(
        sig(side=d, sl=1999.95 if d == 1 else 2000.55),
        tf=tf,
        close_s=start + tf * 60,
        trigger=tick,
    )


@pytest.mark.parametrize("d", [-1, 1])
@pytest.mark.parametrize("tf", [1, 2, 5])
def test_direct_live_input_is_inert_at_dispatch_desk_and_plan(hull_desk, d, tf):
    mt, desk, logs = hull_desk()
    pr = live_sig(d, tf)
    assert (
        pr.side == d
        and pr.entry_reason == "closed_cross_required"
        and not pr.entry_side
    )
    with pytest.raises(Block, match="closed_cross_required"):
        make_plan(
            pr,
            desk.syms[NAME][1],
            Grid.from_info(mt.i),
            mt.q.bid,
            mt.q.ask,
            desk.cash(NAME),
        )
    assert desk.batch(NAME, pr) == "closed_cross_required"
    dispatch(desk, NAME, [pr], **OK)
    assert (
        not mt.requests
        and not desk.waiting
        and NAME not in desk.state.get("consumed", {})
    )
    assert not any(event in ("exit_request", "batch_preview") for event, _ in logs)


def test_old_live_reservation_is_honored_but_no_new_one_is_written(hull_desk):
    state = {"live_bars": {NAME: {"1": [T, 1]}}}
    mt, d, _ = hull_desk(state=state)
    assert d.batch(NAME, sig()) == "already_consumed" and not mt.requests
    assert d.batch(NAME, live_sig()) == "closed_cross_required" and state[
        "live_bars"
    ] == {NAME: {"1": [T, 1]}}


def test_closed_label_benchmark_rejects_live_inputs_instead_of_backdating():
    from bot.hull.replay import Account

    with pytest.raises(ValueError, match="Closed-label benchmark"):
        Account.step(None, None, [live_sig()])
