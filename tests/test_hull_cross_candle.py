"""Confirmed-cross entries and frozen wick protection. In-memory terminal/tape only."""

import json
import sys
from dataclasses import replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from types import SimpleNamespace as NS

import pytest
from hull_mt import MT, ROOT, T, scanner_clock, sig
from pydantic import ValidationError
from test_hull_failed_mss import P, bars

from bot.config.loader import load_yaml
from bot.hull.config import HullConfig, Indicator, selection
from bot.hull.curves import LiveTick
from bot.hull.entry_clock import WAITING
from bot.hull.feed import Stream
from bot.hull.orders import cross_candle_stop
from bot.hull.signal import Hull
from bot.hull.terminal import dispatch, run
from bot.zones.engine import Bar

NAME = "XAUUSDm"
OK = {"data_ok": True, "history_ok": True, "rewarmed": False}


def as_rate(b):
    return {"time": b.t, "open": b.o, "high": b.h, "low": b.l, "close": b.c}


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("tf", [15, 30, 45])
@pytest.mark.parametrize("case", ["confirmed", "vanishes", "late_tick"])
def test_real_scanner_one_close_one_batch_not_the_forming_cross(
    tmp_path, monkeypatch, tf, side, case
):
    """Observe the same crossing twice: a projection before the boundary and shift-1
    on the next bar. M45 is read through the real D1 + M15 adapter, never a fake MT period.
    """
    path = bars(side, tf)
    original = path[-1]
    quiet = original.c - 2 * side
    # The widest excursion is not in the last M15 constituent. This also makes
    # the signal wick different from any previous-pullback or next-bar anchor.
    outer = quiet - 0.5 * side
    last = Bar(
        original.t,
        original.o,
        max(original.h, outer),
        min(original.l, outer),
        original.c,
    )
    successor = original.c + 0.04 * side
    times = [T - 2.0, T - 0.5, T + 0.125, T + 0.4, T + 0.7]
    if case == "late_tick":
        times[-2:] = [T + 8.125, T + 8.4]
    phase = [0]
    wall = lambda: times[phase[0]]

    def endbar():
        c = (
            quiet
            if phase[0] == 0 or (case == "vanishes" and phase[0] >= 2)
            else original.c
        )
        return replace(last, c=c)

    def chart_rows(_name, period, pos, count):
        assert period == (30 if tf == 30 else 15)
        later = Bar(T, successor, successor, successor, successor)
        tape = path[:-1] + [endbar(), later]
        if tf == 45:
            base = []
            for b in tape:
                base.extend(
                    [
                        Bar(b.t, b.o, b.h, b.l, b.o),
                        Bar(b.t + 900, b.o, b.o, b.o, b.o),
                        Bar(
                            b.t + 1800,
                            b.o,
                            max(b.o, b.c, quiet if b.t == last.t else b.o),
                            min(b.o, b.c, quiet if b.t == last.t else b.o),
                            b.c,
                        ),
                    ]
                )
            tape = base
        tape = [b for b in tape if b.t <= wall()]
        return [as_rate(b) for b in (tape[:-1] if pos else tape)[-count:]]

    def days(_name, period, start, end):
        assert period == mt.TIMEFRAME_D1
        day = int(start.timestamp()) // 86400 * 86400
        return [
            {"time": v}
            for v in range(day, int(end.timestamp()) + 1, 86400)
            if v >= start.timestamp()
        ]

    mt = MT()
    mt.copy_rates_from_pos = chart_rows
    mt.copy_rates_range = days
    # A trade tick is 0.025, not the configured pip 0.01 or the broker point .001.
    mt.i.trade_tick_size = 0.025

    def tick(_name):
        before = phase[0] < 2
        mark = endbar().c if before else successor
        # MT reports shift-1 before the newest terminal tick. Do not price an
        # order off this pre-close quote; wait only within the original deadline.
        ms = T * 1000 - 1 if phase[0] == 2 else int(wall() * 1000)
        mt.q = NS(time_msc=ms, bid=mark, ask=mark + 0.05)
        return mt.q

    mt.symbol_info_tick = tick
    mt.initialize = lambda: True
    mt.shutdown = lambda: None
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt)
    scanner_clock(monkeypatch, wall, lambda _: phase.__setitem__(0, phase[0] + 1))
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["run"]["mode"] = "demo"
    cfg["hull"]["eligible_timeframes"] = [f"M{tf}"]
    cfg["hull"]["indicator"]["swing_len"] = 1
    h = HullConfig.model_validate(cfg["hull"])
    stamps = []
    prechecks = []
    send = mt.order_send
    check = mt.order_check

    def capture(req):
        stamps.append(mt.q.time_msc)
        return send(req)

    mt.order_send = capture

    def capture_check(req):
        prechecks.append(dict(req))
        return check(req)

    mt.order_check = capture_check
    run(cfg, h, selection(h, rate="medium"), armed=True, iterations=len(times))
    logs = [
        json.loads(line)
        for line in next(tmp_path.glob("reports/hull_runtime/*/demo.jsonl"))
        .read_text()
        .splitlines()
    ]

    def events(k):
        return [v for v in logs if v["event"] == k]

    assert events("started")[0]["summary"]["entry_timing"] == "confirmed_cross_next_bar"
    live = [e for e in events("live_hull_cross") if e["close_s"] == T]
    assert (
        len(live) == 1
        and live[0]["label"] == side
        and live[0]["entry_side"] == 0
        and live[0]["raw_sl"] is None
    )
    assert live[0]["trigger_msc"] < T * 1000
    closed = [e for e in events("closed_label") if e["close_s"] == T]
    if case == "vanishes":
        assert not closed and not mt.requests and not events("batch_preview")
        return
    (r,) = closed
    assert (
        r["entry_side"] == side
        and r["mss_skip_exception"] == "failed_counter_mss_return"
    )
    assert r["trigger"] is None and r["anchor_s"] == original.t and r["prior_bars"] == 0
    wick = min(original.l, outer) if side == 1 else max(original.h, outer)
    assert r["anchor"] == wick and all(wick not in (b.h, b.l) for b in path[:-1])
    assert (
        r["atr"] > 0
        and r["cross_candle_open_s"] == T - tf * 60
        and r["stop_tick_size"] == 0.025
    )
    # Independent arithmetic: Wilder ATR seeded and updated on completed data only.
    tr = []
    prev = None
    atr = None
    for b in [*path[:-1], last]:
        tr.append(
            max(
                b.h - b.l,
                abs(b.h - prev) if prev is not None else 0,
                abs(b.l - prev) if prev is not None else 0,
            )
        )
        prev = b.c
        if len(tr) == 14:
            atr = sum(tr) / 14
        elif len(tr) > 14:
            atr = (atr * 13 + tr[-1]) / 14
    assert r["atr"] == pytest.approx(atr)
    unit = Decimal(".025")
    pad = max(unit, Decimal(str(0.1 * atr)))
    expected = float(
        ((Decimal(str(wick)) - side * pad) / unit).to_integral_value(
            rounding=ROUND_FLOOR if side == 1 else ROUND_CEILING
        )
        * unit
    )
    assert r["raw_sl"] == r["frozen_cross_sl"] == expected
    assert any(
        v["tick_status"] == "quote_precedes_signal_close" and v["disposition"] == "wait"
        for v in events("entry_quote_check")
    )
    if case == "late_tick":
        assert (
            not mt.positions
            and not prechecks
            and not mt.requests
            and not events("batch_preview")
        )
        assert any(
            v.get("outcome") == "entry_window_expired" for v in events("label_decision")
        )
        return
    assert len(mt.positions) == len(mt.requests) == len(prechecks) == 4
    assert (
        stamps == [int(times[3] * 1000)] * 4
    )  # not the close print, not a replay, not a later batch
    assert prechecks == mt.requests
    assert all(
        q["sl"] == expected
        and q["price"] == (successor + 0.05 if side == 1 else successor)
        and q["tp"] > 0
        and q["comment"].startswith(f"h{tf}_{T}_")
        for q in mt.requests
    )
    assert all(
        v["frozen_cross_sl"] == expected and v["entry_timing"] == "confirmed_close"
        for v in events("demo_result")
    )
    assert [v["outcome"] for v in events("label_decision")] == ["demo_batch_sent"]


@pytest.mark.parametrize(
    "anchor,pad,tick,buy,sell",
    [
        (100, 0, 0.25, 99.75, 100.25),
        (100.12, 0.03, 0.25, 99.75, 100.5),
        (100, 0.1 * 3, 0.01, 99.70, 100.30),
        (1.12345, 0.000137, 0.00005, 1.12330, 1.12360),
    ],
)
def test_trade_tick_floor_and_outward_rounding_cross_candle(
    anchor, pad, tick, buy, sell
):
    assert cross_candle_stop(anchor, 1, pad, tick) == buy
    assert cross_candle_stop(anchor, -1, pad, tick) == sell


@pytest.mark.parametrize("tick", [None, 0, -0.01, float("nan")])
def test_no_pip_or_old_swing_fallback_if_tick_unavailable(tick):
    h = Hull(15, P, 0.01, tick_size=tick)
    for b in bars(1, 15):
        r = h.update(b)
    assert (
        r.label == 1
        and r.entry_side == 0
        and r.raw_sl is None
        and r.entry_reason == "cross_stop_data_unavailable"
    )


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("veto", ["gap", "broker_distance", "grid_change", "risk"])
def test_skip_instead_of_moving_frozen_stop_after_close_or_adding_spread(
    hull_desk, side, veto
):
    mt, d, _ = hull_desk("medium")
    d.h = d.h.model_copy(update={"eligible_timeframes": ("M15",)})
    h = Hull(15, P, 0.01, tick_size=mt.i.trade_tick_size)
    for b in bars(side, 15):
        r = h.update(b)
    assert r.entry_side == side
    sl = r.raw_sl
    if veto == "gap":
        # SELL closes on ask: the bid can still be below the chart's high.
        mt.q.bid = sl if side == 1 else sl - 0.05
        mt.q.ask = mt.q.bid + 0.1
    elif veto == "broker_distance":
        mt.i.trade_freeze_level = 5000
    elif veto == "grid_change":
        mt.i.trade_tick_size = 0.01
    else:
        d.lim = d.lim.model_copy(update={"max_open_risk_usd": 0.001})
    assert (
        d.batch(NAME, r)
        == {
            "gap": "initial_stop_breached_or_too_close",
            "broker_distance": "initial_stop_breached_or_too_close",
            "grid_change": "cross_candle_stop_grid_changed",
            "risk": "portfolio_risk_limit",
        }[veto]
    )
    assert (
        not mt.requests
        and not d.waiting
        and d.batch(NAME, r) == "already_consumed"
        and r.raw_sl == sl
    )


@pytest.mark.parametrize("mode", ["same", "opposite", "sibling"])
def test_live_raw_never_steals_confirmed_first_quote_window(hull_desk, mode):
    mt, d, log = hull_desk("medium", armed=False)
    confirmed = sig()
    q, mt.q = mt.q, None
    assert d.batch(NAME, confirmed) == WAITING
    mt.q = q
    other = replace(
        sig(side=1 if mode == "same" else -1),
        tf=2 if mode == "sibling" else 1,
        trigger=LiveTick.read(q),
        close_s=T + (120 if mode == "sibling" else 60),
    )
    dispatch(d, NAME, [other], **OK)
    assert [v["outcome"] for k, v in log if k == "label_decision"] == [
        "opposite_after_confirmed_cross" if mode == "opposite" else "shadow_plan_only"
    ]
    assert not mt.requests and not d.waiting


def test_old_private_clocks_and_stops_rejected_instead_of_silently_trading_old_rule():
    cfg = load_yaml(ROOT / "config.yaml")["hull"]
    assert HullConfig.model_validate(cfg).indicator == Indicator()
    for old in ["local_pullback", "confirmed_pivot"]:
        with pytest.raises(ValidationError, match="cross_candle"):
            HullConfig.model_validate(
                cfg | {"indicator": cfg["indicator"] | {"stop": old}}
            )
    with pytest.raises(ValidationError, match="confirmed_cross_next_bar"):
        HullConfig.model_validate(cfg | {"entry_timing": "live_hull_cross"})


def test_history_reload_and_late_publication_at_attach_are_not_opportunities():
    mt = MT()
    lane = Stream(15, P, 0.01, tick_size=0.001)
    path = bars(1, 15)
    shown = [path[:-1]]
    mt.copy_rates_from_pos = lambda *a: [as_rate(b) for b in shown[0][-a[-1] :]]
    # Already after the signal close when attached; shift-1 arrives one poll late.
    assert lane.refresh(mt, NAME, T + 0.1) == (None, "warmup_only")
    shown[0] = path
    assert lane.refresh(mt, NAME, T + 0.2) == (None, "warmup_only")
    assert lane.last_label.close_s == T and lane.refresh(mt, NAME, T + 0.3)[0] is None
    # Missing closed history and a recovered signalled candle are baseline-only too.
    lane = Stream(15, P, 0.01, tick_size=0.001)
    shown[0] = path[:-1]
    lane.refresh(mt, NAME, T - 0.5)
    shown[0] = []
    assert lane.refresh(mt, NAME, T - 0.25) == (None, "history_unavailable")
    shown[0] = path
    assert lane.refresh(mt, NAME, T + 0.2) == (None, "warmup_only")


@pytest.mark.parametrize("side", [-1, 1])
def test_new_candle_and_each_new_fill_price_cannot_move_frozen_stop(hull_desk, side):
    from bot.hull.feed import verify_before_leg

    mt, d, events = hull_desk("medium")
    d.h = d.h.model_copy(update={"eligible_timeframes": ("M15",)})
    lane = Stream(15, P, 0.01, tick_size=mt.i.trade_tick_size)
    lane.baseline_due = False
    for b in bars(side, 15):
        closed = lane.h.update(b)
    assert closed.close_s == T and closed.entry_side == side

    def forming(*_):
        # The new candle has very different highs/lows and changes between legs.
        return [
            as_rate(
                Bar(
                    T,
                    2000,
                    max(2000, mt.q.bid) + 0.01,
                    min(2000, mt.q.bid) - 0.01,
                    mt.q.bid,
                )
            )
        ]

    mt.copy_rates_from_pos = forming
    d.verify_signal = lambda name, r, q: verify_before_leg(
        mt,
        name,
        {15: lane},
        r,
        q,
        lambda: T + 0.3,
        4,
        lambda opp: d.close_opposite(name, opp),
    )
    send = mt.order_send

    def move(req):
        res = send(req)
        mt.q = NS(
            bid=round(mt.q.bid + side * 0.01, 3),
            ask=round(mt.q.ask + side * 0.01, 3),
            time_msc=mt.q.time_msc + 10,
        )
        return res

    mt.order_send = move
    assert d.batch(NAME, closed) == "demo_batch_sent"
    assert len({r["price"] for r in mt.requests}) == len(mt.positions) == 4
    assert (
        {r["sl"] for r in mt.requests}
        == {p.sl for p in mt.positions}
        == {closed.raw_sl}
    )
    assert {v["frozen_cross_sl"] for k, v in events if k == "demo_result"} == {
        closed.raw_sl
    }
