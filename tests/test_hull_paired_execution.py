"""Native MT5 clocks and bid candles simulated; no terminal, network, fills or P/L claims.

The timelines exercise the real runner, Hull/structure, desk and journal. The
market tapes below are independently engineered to isolate dual-role routing.
"""

import json
import sys
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_cross_tape import as_rate
from hull_mt import MT, ROOT, T, scanner_clock
from test_hull_failed_mss import P, bars

from bot.broker import DemoSafetyError
from bot.config.loader import load_yaml
from bot.hull.bar import Bar
from bot.hull.config import (
    PAIR_MODE,
    PAIR_POLICY,
    PAIR_STOP,
    PAIRS,
    HullConfig,
    selection,
)
from bot.hull.curves import LiveTick
from bot.hull.orders import cross_candle_stop
from bot.hull.pairs import pair_reading, select
from bot.hull.signal import FirstCrossGate, Hull
from bot.hull.structure import Structure
from bot.hull.terminal import run

NAME = "XAUUSDm"
FOUR_CLOCK_PAIRS = {5: 3, 10: 5, 15: 10}  # dual-role legacy research tapes
SCALE = {
    3: 0.5,
    5: 1,
    10: 1.75,
    15: 2.5,
}  # visibly different native wicks, ATRs and cross thresholds


def tape(
    tmp_path,
    mp,
    *,
    stamp=T,
    parents=(5, 10, 15),
    signs=None,
    case="entry",
    armed=True,
    preflight=False,
    delay=None,
    missing=None,
    neutral=None,
    broker=None,
    route=None,
    on_phase=None,
):
    symbols = sorted(set(parents) | {FOUR_CLOCK_PAIRS[p] for p in parents})
    signs = signs or {}
    times = [stamp - 0.25, stamp + 0.125, stamp + 0.375, stamp + 0.625]
    if case == "late":
        times[1:] = [stamp + 9.1, stamp + 9.3, stamp + 9.5]
    if case == "flicker":
        times = [stamp - 0.8, stamp - 0.25, stamp + 0.125, stamp + 0.375]
    if case == "expiry":
        times[2:] = [stamp + 8.1, stamp + 9]
    phase = [0]
    sent = [False]
    calls = []
    paths = {}
    for tf in symbols:
        end = stamp - stamp % (tf * 60)
        path = bars(signs.get(tf, 1), tf, end=end)

        def scale(v):
            return 2000 + (v - 2000) * SCALE[tf]

        paths[tf] = [Bar(b.t, *(scale(x) for x in (b.o, b.h, b.l, b.c))) for b in path]
        if neutral == tf:
            paths[tf] = [Bar(b.t, 2000, 2000, 2000, 2000) for b in path]

    def now():
        return times[phase[0]]

    def price():
        if case == "flicker":
            return 2000 if phase[0] == 1 else 1997
        if case == "other_then_own":
            return (2000, 2000, 1998, 1995)[phase[0]]
        if case == "interrupt" and sent[0]:
            return 1995
        return 2000.0

    mt = MT() if broker is None else broker
    mt.i.trade_tick_size = 0.025

    def rates(name, tf, pos, count):
        assert (
            name == NAME and tf in symbols
        )  # no M1 or M30/M45/D1 reads, nor unauthorized parents
        calls.append((tf, pos, count, phase[0]))
        period = tf * 60
        if tf == missing:
            return None
        cutoff = (
            now()
            if delay != tf or (phase[0] != 1 and case != "expiry")
            else min(now(), stamp - 0.01)
        )
        x = [b for b in paths[tf] if b.t + period <= cutoff]
        if case == "flicker" and tf == 3 and now() >= stamp:
            x[-1] = Bar(stamp - period, 2000, 2000, 1997, 1997)
        if not pos:
            bid = price()
            op = x[-1].c
            x = [*x, Bar(x[-1].t + period, op, max(op, bid), min(op, bid), bid)]
        return [as_rate(b) for b in x[-count:]]

    def tick(_):
        ms = (
            int(now() * 1000)
            if not (case == "wait" and phase[0] == 1)
            else stamp * 1000 - 1
        )
        mt.q = NS(
            time_msc=ms, bid=price(), ask=price() + (0.75 if case == "spread" else 0.05)
        )
        return mt.q

    def pause(_):
        phase[0] += 1
        if on_phase:
            on_phase(mt, phase[0], now())

    mt.copy_rates_from_pos = rates
    mt.symbol_info_tick = tick
    mt.initialize = lambda: True
    mt.shutdown = lambda: None
    original = mt.order_send

    def submit(req):
        ret = original(req)
        if req.get("position") is None:
            sent[0] = True
        return ret

    mt.order_send = submit
    mp.chdir(tmp_path)
    mp.setenv("HOME", str(tmp_path))
    mp.setenv("USERPROFILE", str(tmp_path))
    mp.setitem(sys.modules, "MetaTrader5", mt)
    scanner_clock(mp, now, pause)
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["hull"].update(pairs=FOUR_CLOCK_PAIRS, entry_timing="confirmed_cross_next_bar")
    cfg["run"]["mode"] = "demo"
    if route:
        cfg["hull"]["entry_route"] = route
    cfg["hull"].update(eligible_timeframes=[f"M{p}" for p in parents])
    cfg["hull"]["indicator"]["swing_len"] = 1
    if case == "mismatch_chart":
        mt.i.chart_mode = 1
    if case == "unverified_deals":
        mt.history_deals_get = lambda *_: None
    h = HullConfig.model_validate(cfg["hull"])
    summary = run(
        cfg,
        h,
        selection(h, rate="medium"),
        armed=armed,
        preflight=preflight,
        iterations=len(times),
    )
    if preflight:
        return mt, summary, paths, calls
    log = next((tmp_path / "reports/hull_runtime").glob("*/*.jsonl"))
    return mt, [json.loads(line) for line in log.read_text().splitlines()], paths, calls


def events(rows, key):
    return [r for r in rows if r["event"] == key]


@pytest.mark.parametrize("parent,minute", [(5, 6), (10, 5), (15, 10)])
@pytest.mark.parametrize("side", [-1, 1])
def test_exact_pair_only_noncoincident_native_clocks_frozen_wick_and_quote(
    tmp_path, monkeypatch, parent, minute, side
):
    stamp = T + minute * 60
    child = FOUR_CLOCK_PAIRS[parent]
    mt, rows, paths, calls = tape(
        tmp_path,
        monkeypatch,
        parents=(parent,),
        stamp=stamp,
        signs={parent: side, child: side},
    )
    assert {tf for tf, _, _, _ in calls} == {parent, child}
    parent_close = stamp - stamp % (parent * 60)
    assert parent_close < stamp and not any(
        tf == parent and pos == 1 and phase > 0 for tf, pos, _, phase in calls
    )
    top = rows[0]["summary"]
    assert (
        top["parent_to_trigger"] == {f"M{parent}": f"M{child}"}
        and top["specs"][NAME]["max_tickets_across_selected_lanes"] == 4
    )
    assert len(mt.positions) == len(mt.requests) == 4
    for r in events(rows, "demo_result"):
        assert (r["tf"], r["parent_tf"], r["trigger_tf"]) == (parent, parent, child)
        assert (
            r["parent_structure_close_s"] == parent_close
            and r["trigger_structure_close_s"] == stamp
        )
        assert (
            r["parent_structure_bias"]
            == r["trigger_structure_bias"]
            == r["entry_side"]
            == side
        )
        assert (
            r["entry_policy"] == PAIR_POLICY and r["initial_stop_policy"] == PAIR_STOP
        )
        assert r["request"]["comment"] == f"p{parent}x{child}_{stamp}_{r['leg']}"
        wick = lambda b: b.l if side == 1 else b.h
        assert r["cross_candle_open_s"] == stamp - child * 60 and r[
            "cross_candle_wick"
        ] == wick(paths[child][-1])
        assert r["cross_candle_wick"] != wick(paths[child][-2]) and r[
            "cross_candle_wick"
        ] != wick(paths[parent][-1])
        assert (
            r["request"]["sl"]
            == pytest.approx(r["raw_chart_sl"] + r["stop_spread_buffer_price"])
            and r["frozen_cross_sl"]
            == cross_candle_stop(
                r["cross_candle_wick"], side, r["cross_atr"] * 0.1, 0.025
            )
        )
        assert (
            r["request"]["deviation"] == 0
            and r["request"]["tp"] > 0
            and r["request"]["price"] == (r["send_ask"] if side == 1 else r["send_bid"])
        )
        assert (
            r["close_to_detect_ms"] == r["close_to_submit_ms"] == 125
            and r["send_tick_time_msc"] == stamp * 1000 + 125
        )
    assert (
        len(events(rows, "pair_arbitration")) == 1
        and len(events(rows, "label_decision")) == 1
    )
    state = json.loads(
        next(tmp_path.glob(".moneyprinterbot/hull/*/state.json")).read_text()
    )
    assert all(
        x["tf"] == parent and x["trigger_tf"] == child and x["pair_policy_key"]
        for x in state["hull_lane_positions_v1"].values()
    )
    assert "m1_consumed" not in state and state["pair_consumed"][NAME] == stamp


@pytest.mark.parametrize("delay", [None, 3, 5, 10, 15])
def test_same_close_arbitrates_once_despite_non_atomic_publication(
    tmp_path, monkeypatch, delay
):
    mt, rows, _, calls = tape(tmp_path, monkeypatch, delay=delay)
    assert {tf for tf, _, _, _ in calls} == {3, 5, 10, 15}
    (decision,) = events(rows, "pair_arbitration")
    assert decision["outcome"] == "selected" and decision["owner_tf"] == 15
    assert [
        (r["parent_tf"], r["trigger_tf"], r["reason"]) for r in decision["candidates"]
    ] == [(5, 3, "ready"), (10, 5, "ready"), (15, 10, "ready")]
    assert bool(events(rows, "pair_close_wait")) == (delay is not None)
    assert len(mt.requests) == 4 and all(
        r["comment"].startswith("p15x10_") for r in mt.requests
    )
    assert {r["close_to_submit_ms"] for r in events(rows, "demo_result")} == {
        125 if not delay else 375
    }


def test_opposed_eligible_pairs_no_trade_even_when_one_arrives_first(
    tmp_path, monkeypatch
):
    mt, rows, _, _ = tape(
        tmp_path, monkeypatch, signs={3: -1, 5: -1, 10: 1, 15: 1}, delay=3
    )
    (r,) = events(rows, "pair_arbitration")
    assert r["outcome"] == "conflicting_simultaneous_pairs" and not r["owner_tf"]
    assert not mt.requests and not events(rows, "batch_preview")


def test_bearish_m5_is_not_an_unrelated_veto(tmp_path, monkeypatch):
    stamp = (
        T + 600
    )  # M3 isn't due; M5's own SELL is NOT eligible with a bullish M10 parent
    mt, rows, _, _ = tape(
        tmp_path, monkeypatch, stamp=stamp, signs={3: -1, 5: -1, 10: 1, 15: 1}
    )
    (d,) = events(rows, "pair_arbitration")
    assert d["owner_tf"] == 15 and d["trigger_tf"] == 10
    assert [
        (r["parent_tf"], r["trigger_bias"], r["parent_bias"], r["reason"])
        for r in d["candidates"]
    ] == [(10, -1, 1, "pair_structure_disagrees"), (15, 1, 1, "ready")]
    assert len(mt.positions) == 4 and all(
        r["request"]["type"] == 0 for r in events(rows, "demo_result")
    )


@pytest.mark.parametrize(
    "case,legs,why",
    [
        ("entry", 4, "demo_batch_sent"),
        ("spread", 0, "spread_cap"),
        ("wait", 4, "demo_batch_sent"),
        ("late", 0, "entry_window_expired"),
        ("expiry", 0, "entry_window_expired"),
        ("unverified_deals", 0, "data_or_deal_history_unavailable"),
        (
            "interrupt",
            1,
            "protected_partial_no_topup:next_bar_quote_or_context_unverified",
        ),
    ],
)
def test_no_chasing_no_fallback_no_topup(tmp_path, monkeypatch, case, legs, why):
    mt, rows, _, _ = tape(
        tmp_path, monkeypatch, case=case, delay=3 if case == "expiry" else None
    )
    assert len([r for r in mt.requests if "position" not in r]) == legs
    assert any(r.get("outcome") == why for r in rows)
    if case == "interrupt":
        assert not mt.positions and any(
            r.get("reason") == "opposite_pair_trigger_hull" for r in rows
        )
    if case == "wait":
        assert any(r.get("disposition") == "wait" for r in rows)
    assert len(events(rows, "pair_arbitration")) == 1


def test_live_m3_flicker_does_not_close_m15_but_own_m10_does(tmp_path, monkeypatch):
    mt, rows, _, _ = tape(tmp_path, monkeypatch, case="other_then_own")
    assert len(mt.requests) == 8 and not mt.positions
    live = [
        r
        for r in events(rows, "pair_trigger_exit_signal")
        if r["phase"] == "live" and r["raw_side"] == -1
    ]
    assert any(
        (r["parent_tf"], r["trigger_tf"], r["matched_positions"], r["trigger_msc"])
        == (5, 3, 0, T * 1000 + 375)
        for r in live
    )
    assert any(
        (r["parent_tf"], r["trigger_tf"], r["matched_positions"], r["trigger_msc"])
        == (15, 10, 4, T * 1000 + 625)
        for r in live
    )
    assert [r["reason"] for r in events(rows, "exit_request")] == [
        "opposite_pair_trigger_hull"
    ] * 4


def test_no_entry_on_disappearing_forming_cross(tmp_path, monkeypatch):
    mt, rows, _, _ = tape(tmp_path, monkeypatch, parents=(5,), case="flicker")
    assert any(
        r["phase"] == "live" and r["raw_side"] == 1
        for r in events(rows, "pair_trigger_exit_signal")
    )
    assert not mt.requests and not events(rows, "closed_label")


@pytest.mark.parametrize(
    "missing,neutral,owner",
    [(10, None, 5), (None, 10, 5), (5, None, 15), (15, None, 10)],
)
def test_neutral_or_unavailable_pair_is_ineligible_not_a_global_vote(
    tmp_path, monkeypatch, missing, neutral, owner
):
    mt, rows, _, _ = tape(tmp_path, monkeypatch, missing=missing, neutral=neutral)
    (d,) = events(rows, "pair_arbitration")
    assert d["owner_tf"] == owner and len(mt.positions) == 4
    assert all(
        r["trigger_tf"] == FOUR_CLOCK_PAIRS[owner] for r in events(rows, "demo_result")
    )


def test_observer_and_preflight_name_both_roles_no_orders(tmp_path, monkeypatch):
    mt, summary, _, _ = tape(tmp_path, monkeypatch, armed=False, preflight=True)
    assert summary["pair_history_ready"] and summary["parent_to_trigger"] == {
        "M5": "M3",
        "M10": "M5",
        "M15": "M10",
    }
    checks = summary["history_check"][NAME]
    assert (
        set(checks) == {"M3", "M5", "M10", "M15"}
        and checks["M5"]["parent"]
        and checks["M5"]["trigger_for"] == ["M10"]
    )
    assert not mt.requests and not (tmp_path / "reports").exists()
    for opts in [dict(missing=3), dict(case="mismatch_chart")]:
        with pytest.raises(DemoSafetyError, match="history unavailable or stale"):
            tape(tmp_path, monkeypatch, armed=False, preflight=True, **opts)
    mt, rows, _, _ = tape(tmp_path, monkeypatch, armed=False)
    assert not mt.requests and [
        r["outcome"] for r in events(rows, "label_decision")
    ] == ["shadow_plan_only"]


def test_parent_and_child_closes_strict_gate_and_failed_return():
    h = Hull(4, P, 0.01, tick_size=0.025)
    raw = [h.update(b) for b in bars(1, 4)][-1]
    sign = raw.label
    assert sign == 1
    for parent_end in (T + 1, T - 300):
        with pytest.raises(ValueError):
            pair_reading(raw, 5, Structure(parent_end, 1), T + 0.1)
    for invalid in (
        replace(raw, structure=Structure(T - 240, sign)),
        replace(raw, trigger=LiveTick(T * 1000, 2000, 2000.1)),
    ):
        with pytest.raises(ValueError):
            pair_reading(invalid, 5, Structure(T, sign), T + 0.1)
    for cb, pb in [(0, 1), (1, 0), (-1, 1), (-1, -1), (1, -1)]:
        wrong = pair_reading(
            replace(raw, structure=Structure(T, cb)), 5, Structure(T, pb), T + 0.1
        )
        assert not wrong.entry_side

    def read(close, hms, failed=False):
        s = Structure(
            close - close % 300,
            sign,
            last_mss_s=hms,
            established_bias=sign,
            failed_counter_mss_s=hms - 600 if failed else None,
        )
        return pair_reading(
            replace(raw, close_s=close, structure=Structure(close, sign)),
            5,
            s,
            close + 0.1,
        )

    for failed in (False, True):
        gate = FirstCrossGate()
        mss = read(T, T, failed)
        at_shift = gate.apply(mss, observe=True)
        assert not at_shift.entry_side and gate.record["bar"] == (None if failed else T)
        # An ordinary qualifying cross ON the shift is already the skip. Here
        # check a separate epoch in which the first qualifying cross comes later.
        gate = FirstCrossGate()
        first = gate.apply(read(T + 240, T, failed), observe=True)
        assert bool(first.entry_side) == failed and first.waived_first_cross == failed
        second = gate.apply(read(T + 480, T, failed), observe=True)
        assert second.entry_side == sign and not second.waived_first_cross
        assert select([wrong, second]) == (second, "selected")


def test_one_coherent_bid_market_and_production_structure_depth(tmp_path, monkeypatch):
    """Same underlying prices on ALL four charts, not four independent handcrafted trends.
    Fixed synthetic prefix; exercise the actual child cross, ordinary M5 dissent and
    M15 failed-counter-MSS return without adding a parent Hull/BOS confirmation.
    """
    from random import Random

    n = 1990
    stamp = T + 600
    base = stamp - n * 60
    rng = Random(0)
    px = 2000.0
    minute = []
    for i in range(n):
        op = px
        px = round(op + rng.choice([-8, -5, -3, -1, 1, 3, 5, 8]) * 0.1, 2)
        minute.append(
            Bar(
                base + i * 60,
                op,
                round(max(px, op) + rng.randrange(1, 3) * 0.1, 2),
                round(min(px, op) - rng.randrange(1, 3) * 0.1, 2),
                px,
            )
        )

    def agg(seq):
        return Bar(
            seq[0].t, seq[0].o, max(b.h for b in seq), min(b.l for b in seq), seq[-1].c
        )

    paths = {
        tf: [agg(minute[i : i + tf]) for i in range(0, n, tf)] for tf in (3, 5, 10, 15)
    }
    clock = [stamp - 0.25]

    def rates(_name, tf, pos, count):
        closed = [b for b in paths[tf] if b.t + tf * 60 <= clock[0]]
        if pos:
            return [as_rate(b) for b in closed[-count:]]
        start = closed[-1].t + tf * 60
        live = (
            agg([b for b in minute if b.t >= start])
            if start < stamp
            else Bar(start, px, px, px, px)
        )
        return [as_rate(live)]

    mt = MT()
    mt.copy_rates_from_pos = rates
    mt.initialize = lambda: True
    mt.shutdown = lambda: None

    def quote(_n):
        mt.q = NS(bid=px, ask=px + 0.05, time_msc=int(clock[0] * 1000))
        return mt.q

    mt.symbol_info_tick = quote
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    scanner_clock(
        monkeypatch, lambda: clock[0], lambda _: clock.__setitem__(0, stamp + 0.125)
    )
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["hull"].update(pairs=FOUR_CLOCK_PAIRS, entry_timing="confirmed_cross_next_bar")
    cfg["run"]["mode"] = "demo"
    h = HullConfig.model_validate(cfg["hull"])
    assert h.indicator.swing_len == 3
    run(cfg, h, selection(h, rate="medium"), armed=True, iterations=2)
    rows = [
        json.loads(s)
        for s in next(tmp_path.glob("reports/hull_runtime/*/demo.jsonl"))
        .read_text()
        .splitlines()
    ]
    (a,) = events(rows, "pair_arbitration")
    assert a["owner_tf"] == 15
    assert [
        (c["trigger_tf"], c["side"], c["trigger_bias"], c["parent_bias"])
        for c in a["candidates"]
    ] == [(5, -1, -1, 1), (10, 1, 1, 1)]
    (d,) = events(rows, "closed_label")
    assert (
        d["mss_skip_exception"] == "failed_counter_mss_return"
        and d["parent_structure_close_s"] == T
    )
    assert all(p.sl < paths[10][-1].l for p in mt.positions) and len(mt.requests) == 4


def test_unavailable_inventory_exit_recovers_only_same_fingerprint_pair_side_and_time(
    hull_desk,
):
    mt, d, _ = hull_desk("medium")
    d.h = d.h.model_copy(
        update={
            "eligible_timeframes": ("M5", "M10", "M15"),
            "execution_mode": PAIR_MODE,
        }
    )
    stamp = T * 1000 + 150

    def pos(ticket, parent, child, side=1, *, sym=NAME, magic=None, ms=stamp - 100):
        p = NS(
            ticket=ticket,
            identifier=ticket,
            symbol=sym,
            magic=d.h.magic if magic is None else magic,
            type=d.typ(side),
            time_msc=ms,
            price_open=2000,
            volume=0.1,
            profit=0,
            sl=1999,
            tp=2001,
        )
        mt.positions.append(p)
        if parent is not None:
            assert d.owners.bind(
                p,
                parent,
                minute_source="low" if child == 1 else None,
                pair_child=child if child not in (None, 1) else None,
            )
        return p

    pos(1, 15, 12)
    pos(2, 5, 4)
    pos(3, 10, 8)
    pos(4, 15, 1)
    pos(5, 15, None)
    pos(6, None, None)
    pos(7, 15, 12, -1)
    pos(8, 15, 12, ms=stamp + 1)
    pos(9, 15, 12, sym="OTHER")
    pos(10, 15, 12, magic=8)
    changed = pos(11, 15, 12)
    changed.price_open = 2000.001
    mt.q.time_msc = stamp + 10
    h = Hull(12, P, 0.01, tick_size=0.025)
    for b in bars(1, 12):
        h.update(b)
    r = h.preview(Bar(T, 2000, 2000, 1997, 1997), LiveTick(stamp, 1997, 1997.05))
    assert r.label == -1
    get = mt.positions_get
    mt.positions_get = lambda **_: None
    assert d.pair_opposite(NAME, 15, r) == 0
    mt.positions_get = get
    _, again, _ = hull_desk("medium", state=json.loads(d.path.read_text()), mt=mt)
    again.h = d.h
    again.supervise()
    assert [q["position"] for q in mt.requests] == [1] and [
        p.ticket for p in mt.positions
    ] == list(range(2, 12))
    again.supervise()
    assert len(mt.requests) == 1 and not again.exits


@pytest.mark.parametrize(
    "change", ["legacy", "m1", "drop_parent", "indicator", "unattributed", "valid"]
)
def test_restart_requires_compatible_manager_and_never_replays(
    tmp_path, monkeypatch, change
):
    mt, _, _, _ = tape(tmp_path, monkeypatch)
    assert len(mt.requests) == 4
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["hull"].update(pairs=FOUR_CLOCK_PAIRS, entry_timing="confirmed_cross_next_bar")
    cfg["run"]["mode"] = "demo"
    cfg["hull"]["indicator"]["swing_len"] = 1
    if change in ("legacy", "m1"):
        cfg["hull"].update(
            execution_mode="parent_close"
            if change == "legacy"
            else "m1_parent_structure",
            eligible_timeframes=["M15"],
        )
    if change == "drop_parent":
        cfg["hull"]["eligible_timeframes"] = ["M5", "M10"]
    if change == "indicator":
        cfg["hull"]["indicator"]["swing_len"] = 4
    if change == "unattributed":
        mt.positions[0].price_open += 0.01
    h = HullConfig.model_validate(cfg["hull"])
    if change == "valid":
        run(cfg, h, selection(h, rate="medium"), armed=True, iterations=1)
    else:
        with pytest.raises(DemoSafetyError, match="while open"):
            run(cfg, h, selection(h, rate="medium"), armed=True, iterations=1)
    assert len(mt.requests) == len(mt.positions) == 4


def test_pair_cli_is_not_authorization_and_cannot_select_m1_30_45(
    tmp_path, monkeypatch
):
    import yaml
    from pydantic import ValidationError

    from bot.cli import main

    calls = []
    monkeypatch.setattr("bot.hull.terminal.run", lambda *a, **kw: calls.append((a, kw)))
    main(
        [
            "hull-preflight",
            "--config",
            str(ROOT / "config.yaml"),
            "--paired-confirm",
            "--timeframe",
            "M10",
        ]
    )
    assert (
        calls[0][0][3] == (10,)
        and calls[0][0][1].execution_mode == PAIR_MODE
        and not calls[0][1]["armed"]
    )
    raw = load_yaml(ROOT / "config.yaml")
    old = raw["hull"] | {
        "execution_mode": "m1_parent_structure",
        "eligible_timeframes": ["M5", "M15", "M30", "M45"],
    }
    path = tmp_path / "old.yaml"
    path.write_text(yaml.safe_dump(raw | {"hull": old}))
    with pytest.raises(ValueError, match="subset"):
        main(["trade-all", "--config", str(path), "--paired-confirm"])
    for invalid in ("M1", "M3", "M30", "M45"):
        with pytest.raises(ValidationError):
            HullConfig.model_validate(raw["hull"] | {"eligible_timeframes": [invalid]})
    with pytest.raises(ValueError, match="not in"):
        main(["trade", "--config", str(ROOT / "config.yaml"), "--timeframe", "M45"])
    with pytest.raises(SystemExit):
        main(
            [
                "trade",
                "--config",
                str(ROOT / "config.yaml"),
                "--paired-confirm",
                "--m1-confirm",
            ]
        )
    assert len(calls) == 1


def test_journal_requires_matched_deals_and_consistent_pair_evidence(
    tmp_path, monkeypatch
):
    from contextlib import closing

    monkeypatch.syspath_prepend(str(ROOT / "HullJournal"))
    monkeypatch.syspath_prepend(str(ROOT / "HullJournal/tests"))
    from fixtures import FakeMT, deal
    from ledger import db_open, iso, make_views, sync, tag

    stamp = T + 600
    mt, rows, _, _ = tape(tmp_path, monkeypatch, stamp=stamp, signs={5: -1})
    broker = FakeMT([], stamp + 180)
    broker.login, broker.host = 1, "t"
    log = next(tmp_path.glob("reports/hull_runtime/*/demo.jsonl"))
    assert tag(f"p15x5_{stamp}_1") is None and tag(f"p30x10_{stamp}_1") is None
    for p in mt.positions:
        broker.ds.extend(
            [
                deal(
                    p.ticket,
                    p.identifier,
                    stamp,
                    0,
                    0,
                    0.1,
                    p.price_open,
                    ms=125,
                    comment="broker rewrote the tag",
                    commission=-0.2,
                ),
                deal(
                    p.ticket + 100,
                    p.identifier,
                    stamp + 20,
                    1,
                    1,
                    0.1,
                    p.tp,
                    profit=2.8,
                    fee=-0.1,
                    magic=0,
                ),
            ]
        )
    # Same close/parent/side, but a historical trade is not a member of a paired setup.
    broker.ds.extend(
        [
            deal(70, 70, stamp, 0, 0, 0.1, 2000, comment=f"h15_{stamp}_1"),
            deal(71, 70, stamp + 20, 1, 1, 0.1, 2001, profit=1),
        ]
    )
    with closing(db_open(tmp_path / "journal.sqlite3")) as db:
        fills, broker.ds = broker.ds, []
        sync(db, broker, broker.at, repo=tmp_path)
        assert not make_views(db)[0]["tickets"]
        broker.ds = fills
        sync(db, broker, broker.at + 1, repo=tmp_path, reconcile=True)
        data = make_views(db)[0]
        assert len(data["tickets"]) == 5 and len(data["setups"]) == 2
        paired = [
            r
            for r in data["setups"] + data["tickets"]
            if r["entry_timing"] == "confirmed_pair_close"
        ]
        assert len(paired) == 5
        assert (
            paired[0]["ticket_count"] == "4"
            and paired[0]["net_profit"] == "10.00"
            and not paired[0]["send_bid"]
        )
        for r in paired:
            assert (
                r["timeframe"],
                r["trigger_timeframe"],
                r["initial_stop_policy"],
                r["setup_type"],
            ) == ("M15", "M10", PAIR_STOP, "paired_structure_cross")
            assert (
                r["parent_structure_close_utc"] == iso(T * 1000)
                and r["signal_bar_open_utc"] == iso((stamp - 600) * 1000)
                and r["signal_close_utc"] == iso(stamp * 1000)
                and r["confirmed_pair_bias"] == "bullish"
            )
        for r in data["tickets"] + data["setups"]:
            if r not in paired:
                assert (
                    r["entry_timing"] == "confirmed_close"
                    and not r["initial_stop_policy"]
                    and not r["setup_type"]
                    and not r["trigger_timeframe"]
                )
    # A forged/wrong child, dissenting structure, future parent or later candle cannot
    # retroactively prove a stop/structure from a correct broker ticket tag alone.
    receipt = events(rows, "demo_result")[0]
    for bad in (
        {"trigger_tf": 3},
        {"trigger_structure_bias": -1},
        {"parent_structure_close_s": stamp + 1},
        {"trigger_structure_close_s": stamp - 600},
        {"send_tick_time_msc": (stamp + 600) * 1000},
        {"close_to_submit_ms": 9000},
        {"close_to_submit_ms": 5000},
        {"stop_tick_size": 5},
    ):
        with closing(db_open(":memory:")) as db:
            log.write_text(json.dumps(receipt | bad) + "\n")
            sync(db, broker, broker.at, repo=tmp_path)
            r = next(
                r
                for r in make_views(db)[0]["tickets"]
                if r["position_id"] == str(receipt["deal"])
            )
            assert (
                not r["initial_stop_policy"]
                and not r["confirmed_pair_bias"]
                and not r["send_bid"]
            )


@pytest.mark.parametrize("reason", ["parent_mss", "parent_cross"])
def test_parent_protection_still_works_with_a_quiet_trigger(hull_desk, reason):
    from hull_mt import sig

    mt, d, logs = hull_desk("medium")
    d.h = d.h.model_copy(
        update={
            "eligible_timeframes": ("M5", "M10", "M15"),
            "execution_mode": PAIR_MODE,
        }
    )
    for ticket, parent in ((11, 15), (12, 10), (13, 5)):
        p = NS(
            ticket=ticket,
            identifier=ticket,
            symbol=NAME,
            magic=d.h.magic,
            type=d.typ(1),
            price_open=2000,
            volume=0.1,
            sl=1999,
            tp=2002,
            profit=0,
            time_msc=(T - 2) * 1000,
        )
        mt.positions.append(p)
        assert d.owners.bind(p, parent, pair_child=PAIRS[parent])
    if reason == "parent_mss":
        assert d.confirmed_shift(NAME, 15, Structure(T, -1, -2)) == 1
    else:
        assert d.close_opposite(NAME, replace(sig(side=-1), tf=15)) == 1
    assert [q["position"] for q in mt.requests] == [11] and [
        p.ticket for p in mt.positions
    ] == [12, 13]
    assert not any(k == "pair_trigger_exit_signal" for k, _ in logs)


def test_occupied_symbol_never_stacks_other_owner_and_does_not_fall_back(hull_desk):
    mt, d, _ = hull_desk("medium")
    d.h = d.h.model_copy(
        update={
            "eligible_timeframes": ("M5", "M10", "M15"),
            "execution_mode": PAIR_MODE,
            "entry_timing": "confirmed_cross_next_bar",
        }
    )
    p = NS(
        ticket=90,
        identifier=90,
        symbol=NAME,
        magic=d.h.magic,
        type=d.typ(1),
        price_open=2000,
        volume=0.1,
        sl=1999,
        tp=2002,
        profit=0,
        time_msc=(T - 2) * 1000,
    )
    mt.positions.append(p)
    assert d.owners.bind(p, 15, pair_child=12)

    def reading(tf):
        h = Hull(PAIRS[tf], P, 0.01, tick_size=0.001)
        raw = [h.update(b) for b in bars(1, PAIRS[tf])][-1]
        return pair_reading(raw, tf, Structure(T, 1), T + 0.1)

    d.verify_signal = lambda *_: True
    assert (
        d.batch(NAME, reading(10)) == "symbol_busy_or_batch_changed" and not mt.requests
    )
    mt.positions.clear()
    _, again, _ = hull_desk("medium", state=json.loads(d.path.read_text()), mt=mt)
    again.h = d.h
    again.verify_signal = d.verify_signal
    assert again.batch(NAME, reading(5)) == "already_consumed" and not mt.requests


def test_unpublishable_peer_does_not_delay_a_proven_pair(tmp_path, monkeypatch):
    mt, rows, _, _ = tape(tmp_path, monkeypatch, case="expiry", delay=3, missing=5)
    (decision,) = events(rows, "pair_arbitration")
    assert decision["owner_tf"] == 15 and not events(rows, "pair_close_wait")
    assert len(mt.requests) == 4 and all(
        r["close_to_submit_ms"] == 125 for r in events(rows, "demo_result")
    )
