"""Failed counter-MSS resumption. Invented candles/quotes only, no market backtest."""

import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_mt import T, sig

from bot.hull.bar import Bar
from bot.hull.config import ENTRY_POLICY, Indicator
from bot.hull.curves import LiveTick
from bot.hull.feed import Stream, sample, verify_before_leg
from bot.hull.signal import FirstCrossGate, Hull
from bot.hull.structure import Structure
from bot.hull.terminal import dispatch

NAME = "XAUUSDm"
P = Indicator(
    swing_len=1
)  # explicitly labelled test fixture; Hull length is the production 16
# Up INIT / up BOS / down MSS. Mirror the whole price path for the screenshot direction.
LEG = [
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


def bars(side, tf, *, bos=False, stall=0, gap=False, end=T):
    mid = (
        [(16, 5, 6)] if bos else []
    )  # strict continuation CLOSE < 7 in the counter leg
    path = [(11, 9, 10)] * 24 + LEG + [(16, 8, 12)] * stall + mid
    before = len(path)
    path += [
        (24, 14, 23),
        (24, 19, 24),
    ]  # wick through and close EQUAL to guard, then strict return MSS
    old = 24
    for c in [
        22,
        19,
        16,
        12,
        10,
        11,
        12,
        13,
        16,
        18,
    ]:  # pullback cross before a new continuation BOS
        path.append((max(old, c) + 0.1, min(old, c) - 0.1, c))
        old = c

    def px(v):
        return 2000 + side * (v - 18) / 8

    result = []
    for i, (hi, lo, close) in enumerate(path):
        t = end - (len(path) - i + int(gap and i < before)) * tf * 60
        h, l = sorted((px(hi), px(lo)), reverse=True)
        result.append(Bar(t, (h + l) / 2, h, l, px(close)))
    return result


def read(side, tf, **opts):
    h = Hull(tf, P, 0.01, tick_size=0.001)
    return [h.update(b) for b in bars(side, tf, **opts)]


def ready(side, bar_end, context, *, ms=None, tf=15):
    return replace(
        sig(1999 if side == 1 else 2001, side),
        close_s=bar_end,
        tf=tf,
        structure=context,
        trigger=None if ms is None else LiveTick(ms, 2000, 2000.1),
    )


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("tf", [15, 30, 45])
@pytest.mark.parametrize("bos", [False, True])
def test_real_candles_real_cross_counter_bos_is_the_only_difference(side, tf, bos):
    readings = read(side, tf, bos=bos)
    events = [r for r in readings if r.structure.event_code]
    assert [r.structure.event_code * side for r in events] == [3, 1, -2] + (
        [-1] if bos else []
    ) + [2]
    counter, back = events[2], events[-1]
    assert not counter.structure.failed_return and back.structure.failed_return is (
        not bos
    )
    # A wick across the threshold and an equal close do NOT restore the old trend.
    i = readings.index(back)
    equal = readings[i - 1]
    assert (
        equal.close == equal.structure.guard.price
        and equal.structure.bias == -side
        and not equal.confirmed_mss
    )
    assert not any(
        r.candidate_reason == "ready"
        for r in readings
        if counter.close_s <= r.close_s < back.close_s
    )
    first = readings[-1]
    assert first.raw_sl is not None and first.label == side
    assert all(
        r.structure.event_code == 0 for r in readings[i + 1 :]
    )  # no hindsight BOS
    assert [r.close_s for r in readings[i + 1 :] if r.candidate_reason == "ready"] == [
        T
    ]
    if bos:
        assert (
            first.entry_reason == "first_trend_cross_after_mss"
            and first.entry_side == 0
        )
        assert first.entry_audit()["mss_skip_exception"] is None
    else:
        assert (first.entry_side, first.entry_reason, first.waived_first_cross) == (
            side,
            "ready",
            True,
        )
        audit = first.entry_audit()
        assert (audit["failed_counter_mss_s"], audit["return_mss_s"]) == (
            counter.close_s,
            back.close_s,
        )
        assert (
            audit["mss_skip_exception"] == "failed_counter_mss_return"
            and audit["entry_policy"] == ENTRY_POLICY
        )


@pytest.mark.parametrize("side", [-1, 1])
def test_no_bar_limit_but_missing_counter_history_is_not_evidence(side):
    long = read(side, 15, stall=1501)
    first = long[-1]
    assert first.waived_first_cross and first.entry_side == side
    assert (first.structure.epoch - first.structure.failed_counter_mss_s) // 900 > 1500
    broken = read(side, 15, gap=True)[-1]
    assert (
        broken.label == side
        and not broken.waived_first_cross
        and broken.entry_reason == "first_trend_cross_after_mss"
    )


@pytest.mark.parametrize("side", [-1, 1])
def test_whipsaws_never_waive_wrong_direction_and_bos_not_needed_to_resume(side):
    h = Hull(15, P, 0.01, tick_size=0.001)
    for b in bars(side, 15):
        r = h.update(b)
    assert r.waived_first_cross
    for step in range(1, 7):
        old = h.structure.view
        o = h.prev.c
        c = old.guard.price - 0.5 * old.bias
        r = h.update(Bar(h.prev.t + 900, o, max(o, c), min(o, c), c))
        new = r.structure
        assert new.event_code == -2 * old.bias and new.established_bias == side
        back = step % 2 == 0
        assert new.failed_return is back
        assert new.failed_counter_mss_s == (old.epoch if back else None)
        # The new candle's verified direction-aligned cross after the shift, not the MSS close.
        gate = FirstCrossGate()
        later = ready(new.bias, r.close_s + 900, new)
        assert gate.apply(later, observe=True).entry_side == (side if back else 0)


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("counter_signal", [False, True])
def test_only_fresh_valid_cross_after_return_not_wick_wrong_side_or_same_close(
    side, counter_signal
):
    rows = read(side, 15)
    away, back = [r.structure for r in rows if r.confirmed_mss]
    g = FirstCrossGate()
    x = ready(-side, away.closed_s + 900, away)
    if counter_signal:
        assert g.apply(x, observe=True).entry_reason == "first_trend_cross_after_mss"
        assert (
            g.apply(
                replace(
                    x,
                    close_s=x.close_s + 900,
                ),
                observe=True,
            ).entry_side
            == -side
        )
    before = g.apply(
        ready(side, away.closed_s + 900, away, ms=away.closed_s * 1000 + 2),
        observe=True,
    )
    assert (
        before.entry_reason == "opposite_signal_exit_only"
        and not before.waived_first_cross
    )
    at = ready(side, back.closed_s, back)
    assert g.apply(at, observe=True).entry_reason == "mss_close_exit_only"
    first = replace(
        at, close_s=at.close_s + 900, trigger=LiveTick(at.close_s * 1000, 2000, 2000.1)
    )
    assert g.apply(first, observe=True).entry_reason == "await_cross_candle_close"
    assert g.record["bar"] is None
    assert (
        g.apply(
            replace(first, trigger=replace(first.trigger, ms=first.trigger.ms + 1)),
            observe=True,
        ).entry_side
        == 0
    )
    first = replace(
        first, trigger=None
    )  # a later confirmed close, not a second live observation
    bad = replace(first, side=0, raw_sl=None, reason="no_known_swing_or_atr")
    assert (
        g.apply(bad, observe=True).entry_reason == "no_known_swing_or_atr"
        and g.record["bar"] is None
    )
    wrong = replace(first, side=-side, label=-side)
    assert not g.apply(wrong, observe=True).entry_side and g.record["bar"] is None
    preview = g.apply(first, observe=False)
    assert (
        preview.entry_side == side and g.record["bar"] is None
    )  # a projection does not reserve
    assert g.apply(first, observe=True) == preview
    for again in (first, replace(first, structure=replace(first.structure))):
        assert g.apply(
            again, observe=True
        ).waived_first_cross  # re-verification, not another order
    later = replace(first, close_s=first.close_s + 900, trigger=None)
    assert (
        g.apply(later, observe=True).entry_side == side
        and not g.apply(later, observe=True).waived_first_cross
    )


@pytest.mark.parametrize("side", [-1, 1])
def test_restart_reserved_decisions_and_legacy_skip_cannot_be_refunded(side):
    v = read(side, 15)[-1]
    s = v.structure
    g = FirstCrossGate()
    store = {}
    writes = []
    g.bind(store, lambda: writes.append(json.loads(json.dumps(store))))
    g.apply(v, observe=True)
    g.flush()
    assert writes[-1]["waived_from"] == s.failed_counter_mss_s
    recovered = FirstCrossGate()
    recovered.bind(writes[-1].copy(), None)
    assert recovered.apply(v, observe=False).entry_side == side
    unknown = ready(
        side, T, Structure(T, side)
    )  # no replay evidence of the counter-leg
    assert recovered.apply(unknown, observe=False).entry_side == 0
    old = ready(side, T - 900, replace(s, closed_s=T - 3600, last_mss_s=s.epoch - 900))
    assert (
        not recovered.apply(old, observe=True).entry_side
        and recovered.record == writes[-1]
    )
    # A pre-upgrade v2 journal entry remains a veto on its original bar.
    legacy = FirstCrossGate()
    legacy.bind({"bias": side, "epoch": s.epoch, "bar": T}, None)
    assert legacy.apply(v, observe=True).entry_reason == "first_trend_cross_after_mss"
    assert legacy.apply(replace(v, close_s=T + 900), observe=True).entry_side == side
    # A different lane's journal/structure cannot supply this one's proof.
    other = FirstCrossGate()
    fresh = ready(-side, T, Structure(T - 900, -side, -2 * side), tf=30)
    assert (
        other.apply(fresh, observe=True).entry_side == 0
        and other.record != recovered.record
    )


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("mode", ["observe", "mock_demo", "outside", "wide_spread"])
def test_fresh_resumption_through_confirmed_wick_and_next_quote_real_desk(
    hull_desk, monkeypatch, side, mode
):
    end = (
        T + 3 * 3600 if mode == "outside" else T
    )  # quiet price fixture, same signal other hour
    mt, d, log = hull_desk("medium", armed=mode != "observe")
    d.h = d.h.model_copy(update={"eligible_timeframes": ("M15", "M30", "M45")})
    lane = Stream(15, P, 0.01, tick_size=mt.i.trade_tick_size)
    path = bars(side, 15, end=end)
    history = [lane.h.update(b) for b in path[:-1]]
    back = [r for r in history if r.confirmed_mss][-1]
    wall = [back.close_s + 0.2]
    monkeypatch.setattr("bot.hull.desk.time.time", lambda: wall[0])
    mt.q.time_msc = back.close_s * 1000 + 100
    if mode != "observe":
        # An older countertrend ticket on this lane MUST still be flattened by the return MSS.
        held = NS(
            symbol=NAME,
            magic=d.h.magic,
            ticket=112,
            identifier=112,
            type=d.typ(-side),
            time_msc=(back.close_s - 1) * 1000,
            price_open=2000.0,
            volume=0.1,
            sl=1999.0,
            tp=2001.0,
            profit=0.0,
        )
        mt.positions.append(held)
        assert d.owners.bind(held, 15)
    dispatch(d, NAME, [], data_ok=True, history_ok=True, rewarmed=False, shifts=[back])
    assert not mt.positions
    assert [r["position"] for r in mt.requests] == ([] if mode == "observe" else [112])
    mt.requests.clear()
    last = path[-1]
    mt.q.bid = last.c
    mt.q.ask = last.c + (1 if mode == "wide_spread" else 0.1)
    mt.q.time_msc = end * 1000 - 300
    wall[0] = end - 0.2
    mt.copy_rates_from_pos = lambda *_: [
        {"time": last.t, "open": last.o, "high": last.h, "low": last.l, "close": last.c}
    ]
    lane.baseline_due = False

    def verify(n, r, q):
        return verify_before_leg(
            mt,
            n,
            {15: lane},
            r,
            q,
            lambda: wall[0],
            4,
            lambda opp: d.close_opposite(n, opp),
        )

    d.verify_signal = verify
    readings, ok = sample(mt, NAME, [lane], lambda: wall[0], 4)
    (developing,) = readings
    assert (
        ok
        and developing.trigger
        and developing.label == side
        and developing.entry_side == 0
    )
    assert developing.raw_sl is None and not developing.waived_first_cross
    dispatch(d, NAME, readings, data_ok=True, history_ok=True, rewarmed=False)
    assert not mt.positions and not mt.requests and not d.waiting
    first = lane.h.update(last)
    assert lane.note(first) and first.entry_side == side and first.waived_first_cross
    wall[0] = end + 0.2
    mt.q.time_msc = end * 1000 + 100
    mt.copy_rates_from_pos = lambda *_: [
        {
            "time": end,
            "open": mt.q.bid,
            "high": mt.q.bid,
            "low": mt.q.bid,
            "close": mt.q.bid,
        }
    ]
    dispatch(d, NAME, [first], data_ok=True, history_ok=True, rewarmed=False)
    (decision,) = [v for e, v in log if e == "label_decision"]
    expected = {
        "observe": "shadow_plan_only",
        "mock_demo": "demo_batch_sent",
        "outside": "outside_symbol_session",
        "wide_spread": "spread_cap",
    }
    assert (
        decision["outcome"] == expected[mode]
        and decision["mss_skip_exception"] == "failed_counter_mss_return"
    )
    assert len(mt.requests) == len(mt.positions) == (4 if mode == "mock_demo" else 0)
    assert all(
        r["sl"] > 0 and r["tp"] > 0 and r["type"] == d.typ(side) for r in mt.requests
    )
    assert all(
        v["mss_skip_exception"] == "failed_counter_mss_return"
        for e, v in log
        if e == "demo_result"
    )
    assert sample(mt, NAME, [lane], lambda: wall[0], 4) == ([], True)
    assert (
        d.batch(NAME, first) == "already_consumed"
    )  # no late catch-up or repeat fill after a veto


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("bos", [False, True])
def test_default_swing_three_and_clean_restart_on_same_native_sequence(side, bos):
    # Two native candles per move allow the production three-right pivot to confirm.
    path = [b for b in bars(side, 15, bos=bos, stall=1) for _ in range(2)]
    path = [replace(b, t=T + (j - len(path)) * 900) for j, b in enumerate(path)]

    def load(gate):
        h = Hull(15, Indicator(), 0.01, gate, tick_size=0.001)
        return [h.update(b) for b in path]

    g = FirstCrossGate()
    rows = load(g)
    assert [r.structure.event_code * side for r in rows if r.structure.event_code] == [
        3,
        1,
        -2,
    ] + ([-1] if bos else []) + [2]
    (first,) = [r for r in rows if r.candidate_reason == "ready"]
    assert (
        first.entry_side == (0 if bos else side) and first.waived_first_cross is not bos
    )
    # Replay the closed prefix on top of the saved account/lane marker. A historical
    # warm-up may describe this same candidate but must not change the reservation.
    record = json.loads(json.dumps(g.record))
    restored = FirstCrossGate()
    restored.bind(record.copy(), None)
    replay = load(restored)
    again = next(r for r in replay if r.close_s == first.close_s)
    assert (
        again.entry_reason == first.entry_reason
        and again.waived_first_cross == first.waived_first_cross
    )
    assert restored.record == record


@pytest.mark.parametrize("side", [-1, 1])
def test_original_direction_bos_after_return_does_not_rearm_before_first_cross(side):
    h = Hull(15, P, 0.01, tick_size=0.001)
    for b in bars(side, 15):
        back = h.update(b)
        if back.confirmed_mss and back.structure.failed_return:
            break
    assert back.confirmed_mss and back.structure.failed_return

    def px(v):
        return 2000 + side * (v - 18) / 8

    for hi, lo, c in [(27, 23, 26), (26, 24, 25), (29, 25, 28)]:
        high, low = sorted((px(hi), px(lo)), reverse=True)
        r = h.update(Bar(h.prev.t + 900, (high + low) / 2, high, low, px(c)))
        assert not r.label
    assert (
        r.structure.event_code == side
        and r.structure.epoch == back.close_s
        and h.gate.record["bar"] is None
    )
    after = ready(side, r.close_s + 900, r.structure)
    assert h.gate.apply(after, observe=True).waived_first_cross
