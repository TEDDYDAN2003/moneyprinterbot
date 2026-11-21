"""Dynamic Pine/MT5 wiring contracts. Hand-built candles and fake quotes only, no P&L study."""

import json
import pickle
import sys
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_mt import MT, ROOT, T, scanner_clock
from bot.config.loader import load_yaml
from bot.hull.config import HullConfig, Indicator, selection
from bot.hull.curves import LiveTick
from bot.hull.feed import Stream, StructureTape, sample
from bot.hull.signal import Hull
from bot.hull.structure import InternalStructure, Pivot, Structure
from bot.hull.terminal import run
from bot.zones.engine import Bar

# Exact wicks, equal closes, then a close through the previous, known guard.
PATH = [
    (11, 9, 10),
    (14, 10, 13),
    (12.8, 8, 9),
    (13, 9, 12),
    (15, 11, 14),
    (16, 13, 15),
    (19, 14, 17),
    (18, 13, 15),
    (17, 11, 13),
    (19, 12, 18),
    (20.5, 15, 19),
    (23, 16, 21),
    (23, 10, 12),
    (20, 10, 11),
    (13, 9, 10),
    (14, 7, 8),
    (17, 8, 16),
]
NAME = "XAUUSDm"


def fixture(invert=False, path=PATH):
    def px(x):
        return 200 - x if invert else x

    out = []
    for i, (hi, lo, c) in enumerate(path):
        out.append(
            Bar(
                T + (i - len(path)) * 60,
                px((hi + lo) / 2),
                px(lo if invert else hi),
                px(hi if invert else lo),
                px(c),
            )
        )
    return out


def rate(b):
    return dict(time=b.t, open=b.o, high=b.h, low=b.l, close=b.c)


@pytest.mark.parametrize("invert", [False, True])
@pytest.mark.parametrize("stop", ["local_pullback", "confirmed_pivot"])
def test_native_forming_cross_is_live_but_cannot_modify_closed_trend_anchor_or_atr(
    invert, stop
):
    p = Indicator(hull_len=4, swing_len=1, atr_length=2, stop=stop)
    h = Hull(1, p, 0.01)
    rows = [h.update(b) for b in fixture(invert)]
    sign = -1 if invert else 1
    assert [
        (i, r.structure.event_code * sign)
        for i, r in enumerate(rows)
        if r.structure.event_code
    ] == [(5, 3), (11, 1), (14, -2)]
    assert rows[13].structure.guard.price == (189 if invert else 11)
    assert rows[14].active_source == ("low" if invert else "high")
    # A switch bar has opposite B/A signs across selected values, but no true same-source cross.
    assert (
        not rows[14].label and (rows[13].b - rows[13].a) * (rows[14].b - rows[14].a) < 0
    )
    before = pickle.dumps(h)
    cur = h.prev.c
    px = cur - sign
    b = Bar(T, cur, max(cur, px), min(cur, px), px)
    q = LiveTick(T * 1000 + 300, px, px + 0.01)
    r = h.preview(b, q)
    assert r.label == r.side == -sign and r.structure == rows[-1].structure
    assert r.entry_side == 0 and r.entry_reason == "first_trend_cross_after_mss"
    assert r.atr == rows[-1].atr and r.anchor_s < T and r.trigger == q
    assert r.raw_sl == pytest.approx(r.anchor - r.label * 0.1 * r.atr)
    assert h.preview(replace(b, c=cur), replace(q, bid=cur, ask=cur + 0.01)).label == 0
    assert pickle.dumps(h) == before


@pytest.mark.parametrize("invert", [False, True])
def test_guard_promotion_on_failed_counter_swing_before_bos(invert):
    # Construct a known pullback. A subsequent confirmed pivot tightens the guard
    # even without a new continuation BOS (a distinguishing rule of the pasted Pine).
    d = -1 if invert else 1
    px = lambda x: 200 - x if invert else x
    s = InternalStructure(1)
    s.view = Structure(bias=d)
    s.guard = Pivot(px(90), T - 600, T - 480)
    s.leg_tip = Pivot(px(116), T - 240, T - 180)
    pull = Pivot(px(99), T - 180, T - 120)
    if invert:
        s.pull_h = pull
    else:
        s.pull_l = pull
    for i, (h, l, c) in enumerate([(110, 105, 108), (113, 107, 110), (112, 106, 109)]):
        b = Bar(
            T + 60 * i, px(c), px(l if invert else h), px(h if invert else l), px(c)
        )
        v = s.update(b, b.t + 60)
    assert not v.event_code and v.guard == pull
    b = Bar(
        T + 180, px(100), px(97 if invert else 101), px(101 if invert else 97), px(98)
    )
    v = s.update(b, b.t + 60)
    assert v.event_code == -2 * d and v.broken == pull


def test_break_window_accepts_pivot_left_of_break_but_after_break_origin():
    s = InternalStructure(2)
    s.view = Structure(bias=1)
    s.guard = Pivot(80, T - 600, T - 540)
    s.hi = Pivot(110, T - 360, T - 240)
    s.leg_tip = s.hi
    for i, (hi, close) in enumerate(
        [(105, 100), (106, 101), (120, 109), (116, 111), (115, 109)]
    ):
        b = Bar(T + i * 60, 100, hi, 99, close)
        v = s.update(b, b.t + 60)
    assert (
        v.ph.price == 120
        and v.hi_class == 1
        and s.hi == v.ph
        and s.hi.bar_s < s.leg_start
    )


def test_asof_alignment_never_reads_a_later_structure_close():
    tape = StructureTape(5, 3, 15, 7)
    tape.ready = True
    tape.ends = [T - 300, T, T + 300]
    tape.views = [Structure(t, b) for t, b in zip(tape.ends, [-1, 1, -1])]
    assert tape.at(1, T - 60, T).bias == -1  # HTF at chart open, not chart close
    assert tape.at(1, T, T + 60).bias == 1
    assert tape.at(15, T, T + 299.999).bias == 1 and tape.at(15, T, T + 300).bias == -1
    assert not tape.current(15, T, T + 601)  # never guess a missing native close


def test_initial_live_snapshot_and_repeated_markers_are_not_new_orders():
    lane = Stream(1, Indicator(hull_len=4, swing_len=1, atr_length=2), 0.01)
    for b in fixture():
        lane.h.update(b)
    mt = MT()
    mt.q = NS(time_msc=T * 1000 + 100, bid=15.0, ask=15.01)
    mt.copy_rates_from_pos = lambda *a: [rate(Bar(T, 16, 16, 15, mt.q.bid))]
    assert sample(mt, NAME, [lane], lambda: T + 0.2, 4) == ([], True)
    mt.q.time_msc += 10
    assert sample(mt, NAME, [lane], lambda: T + 0.2, 4) == ([], True)
    # Even closing the same real cross cannot rearm the latching signal.
    assert not lane.note(lane.h.update(Bar(T, 16, 16, 15, 15)))


@pytest.mark.parametrize("invert", [False, True])
@pytest.mark.parametrize("already_skipped", [False, True])
def test_observer_uses_verified_live_chart_candle_not_next_bar_close(
    invert, already_skipped, tmp_path, monkeypatch
):
    # Two observations in one forming candle. Run the actual scanner in READ ONLY,
    # against a fake in-memory terminal; no real terminal, order send, or simulation of returns.
    mt = MT()
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["hull"]["eligible_timeframes"] = ["M1"]
    cfg["hull"]["indicator"].update(swing_len=1, atr_length=2)
    cfg["hull"]["symbols"]["XAUUSD"].update(session="continuous")
    h = HullConfig.model_validate(cfg["hull"])
    # Exercise the production 16-period clock. Warm the curves, flip structure,
    # then either present the first aligned cross or consume it during warmup.
    path = [(11, 9, 10)] * 20 + PATH
    for c in [16] * 5 + [14] + ([13, 12, 14, 16, 17] if already_skipped else []):
        o = path[-1][2]
        path.append((max(o, c), min(o, c), c))
    bars = fixture(invert, path)
    cur = bars[-1].c
    wall = [T + 0.1]

    def quote():
        x = cur if wall[0] < T + 0.2 else cur + (4 if invert else -4)
        return NS(time_msc=int(wall[0] * 1000), bid=x, ask=x + 0.01)

    def rates(_name, tf, shift, n):
        assert tf == mt.TIMEFRAME_M1
        if shift == 1:
            return [rate(x) for x in bars[-n:]]
        x = quote().bid
        return [rate(Bar(T, cur, max(cur, x), min(cur, x), x))]

    mt.copy_rates_from_pos = rates
    mt.symbol_info_tick = lambda n: quote()
    mt.initialize = lambda: True
    mt.shutdown = lambda: None
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt)
    monkeypatch.chdir(tmp_path)
    scanner_clock(
        monkeypatch, lambda: wall[0], lambda dt: wall.__setitem__(0, wall[0] + dt)
    )
    run(cfg, h, selection(h), iterations=3)
    events = [
        json.loads(l)
        for l in next(tmp_path.glob("reports/hull_runtime/*/shadow.jsonl"))
        .read_text()
        .splitlines()
    ]
    (raw,) = [r for r in events if r["event"] == "live_hull_cross"]
    assert raw["label"] == raw["side"] == (1 if invert else -1)
    assert raw["entry_side"] == (raw["label"] if already_skipped else 0)
    assert raw["entry_reason"] == (
        "ready" if already_skipped else "first_trend_cross_after_mss"
    )
    assert (
        raw["trigger_msc"] < raw["close_s"] * 1000
        and raw["entry_timing"] == "live_hull_cross"
    )
    assert [r["outcome"] for r in events if r["event"] == "label_decision"] == [
        "shadow_plan_only" if already_skipped else "first_trend_cross_after_mss"
    ]
    assert not mt.requests


def test_opposite_on_reprice_interrupts_remaining_legs_immediately():
    from bot.hull.feed import verify_before_leg
    from hull_mt import sig

    lane = Stream(1, Indicator(hull_len=4, swing_len=1, atr_length=2), 0.01)
    for b in fixture():
        lane.h.update(b)
    # Last closed native candle: bearish trend and a raw BUY; current tick crosses DOWN.
    mt = MT()
    mt.q = NS(time_msc=T * 1000 + 250, bid=15.0, ask=15.01)
    mt.copy_rates_from_pos = lambda *a: [rate(Bar(T, 16, 16, 15, 15))]
    lane.baseline_due = False
    exits = []
    assert not verify_before_leg(
        mt, NAME, {1: lane}, sig(), mt.q, lambda: T + 0.3, 4, exits.append
    )
    assert (
        len(exits) == 1
        and exits[0].label == -1
        and exits[0].trigger.ms == mt.q.time_msc
    )


@pytest.mark.parametrize("neutral", ["high", "wait"])
def test_dynamic_selects_only_one_complete_source_including_flip_or_neutral(neutral):
    # Two separate single-source controls. Exercise the *default* smoothing length,
    # alternating contexts; the other curve's crossover is never an independent vote.
    h = Hull(1, Indicator(neutral_mode=neutral), 0.001)
    lo = Hull(1, Indicator(source="low"), 0.001)
    hi = Hull(1, Indicator(source="high"), 0.001)
    flat = Hull(1, Indicator(source="custom", custom_source="ohlc4"), 0.001)
    ohlc = Hull(1, Indicator(source="ohlc4"), 0.001)
    seed = 7341
    labels = 0
    rejected = 0

    def step(n):
        nonlocal seed
        seed = seed * 48271 % 2147483647
        return seed % n

    price = 123000
    for i in range(180):
        o = price
        price = o + step(71) - 35
        b = Bar(
            T + i * 60,
            o / 1000,
            (max(o, price) + step(20)) / 1000,
            (min(o, price) - step(20)) / 1000,
            price / 1000,
        )
        bias = (0, 1, -1)[(i // 4) % 3]
        v = Structure(b.t + 60, bias)
        l, r = lo.update(b, context=v), hi.update(b, context=v)
        a = h.update(b, context=v)
        x = flat.update(b, context=v)
        y = ohlc.update(b, context=v)
        assert x.a == y.a and x.label == y.label
        assert l.a == a.a_low and r.a == a.a_high and a.b == l.b == r.b
        expected = l if bias == 1 else r if bias == -1 or neutral == "high" else None
        assert (a.a, a.label) == (
            (expected.a, expected.label) if expected else (None, 0)
        )
        assert a.entry_side in (0, bias)
        labels += bool(a.label)
        rejected += bool((l.label or r.label) and not a.label)
    assert labels and rejected
