"""One-shot veto contracts; synthetic signals, bars and a fake terminal only."""

import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_mt import MT, T, sig

from bot.hull.config import Indicator
from bot.hull.feed import Stream, sample
from bot.hull.signal import FirstCrossGate
from bot.hull.structure import Structure
from bot.hull.terminal import dispatch

NAME = "XAUUSDm"


def candidate(d, time, context):
    return replace(sig(1999 if d == 1 else 2001, d), close_s=time, structure=context)


@pytest.mark.parametrize("d", [1, -1])
def test_only_one_valid_cross_per_mss_even_at_flip_close_or_repeated_ticks(d):
    g = FirstCrossGate()
    # INIT, BOS and countertrend crosses are not independent entry vetoes.
    for code in (3, 1, 0):
        assert (
            g.apply(candidate(d, T, Structure(T, d, d * code)), observe=True).entry_side
            == d
        )
    assert g.record == {}
    flip = Structure(T + 60, -d, -2 * d)
    wrong = candidate(d, T + 60, flip)
    assert g.apply(wrong, observe=True).entry_reason == "opposite_signal_exit_only"
    assert g.record["bar"] is None  # armed but NOT consumed by a wrong-side cross
    notready = replace(
        candidate(-d, T + 60, flip), side=0, raw_sl=None, reason="no_known_swing_or_atr"
    )
    assert g.apply(notready, observe=True).entry_reason == "no_known_swing_or_atr"
    assert g.record["bar"] is None
    r = candidate(-d, T + 60, flip)
    assert g.apply(r, observe=False).entry_side == 0 and g.record["bar"] is None
    for _ in range(3):
        skipped = g.apply(r, observe=True)
        assert (
            skipped.label == -d
            and skipped.entry_reason == "first_trend_cross_after_mss"
        )
    assert g.record["bar"] == T + 60
    # Repeated higher-TF publication, then BOS/quiet: no rearming.
    for k, s in enumerate(
        (
            flip,
            Structure(T + 180, -d, -d, last_mss_s=T + 60),
            Structure(T + 240, -d, last_mss_s=T + 60),
        )
    ):
        assert (
            g.apply(candidate(-d, T + 120 + 60 * k, s), observe=True).entry_side == -d
        )
    # Even without a continuation BOS the next leg has its own one-shot.
    flip2 = Structure(T + 300, d, 2 * d)
    assert g.apply(candidate(d, T + 300, flip2), observe=True).entry_side == 0
    assert g.apply(candidate(d, T + 360, flip2), observe=True).entry_side == d


def test_pending_and_spent_survive_restart_and_a_historical_reload_not_shared_by_lanes():
    v = Structure(T, 1, 2)
    r = candidate(1, T + 60, v)
    store, writes = {}, []
    a = FirstCrossGate()
    a.bind(store, lambda: writes.append(json.loads(json.dumps(store))))
    a.apply(replace(r, label=0), observe=True)
    a.flush()
    assert writes[-1] == {"epoch": T, "bias": 1, "bar": None}
    later = candidate(
        1, T + 120, Structure(T + 120, 1)
    )  # old MSS not in reloaded window
    b = FirstCrossGate()
    b.bind(writes[-1].copy(), None)
    assert b.apply(later, observe=True).entry_side == 0
    assert b.apply(replace(later, close_s=T + 180), observe=True).entry_side == 1
    a.apply(r, observe=True)
    a.flush()
    a.apply(r, observe=True)
    a.flush()
    assert len(writes) == 2
    c = FirstCrossGate()
    c.bind(writes[-1].copy(), None)
    assert not c.apply(
        candidate(-1, T - 60, Structure(T - 60, -1, -2)), observe=True
    ).entry_side
    assert not c.apply(r, observe=True).entry_side  # restart, same event
    assert c.apply(later, observe=True).entry_side == 1
    assert c.record == writes[-1]
    other_lane = FirstCrossGate()
    assert other_lane.apply(replace(r, tf=15), observe=True).entry_side == 0


@pytest.mark.parametrize("d", [-1, 1])
def test_first_aligned_signal_still_closes_owned_opposite_but_never_opens(hull_desk, d):
    mt, desk, log = hull_desk()
    side = mt.POSITION_TYPE_BUY if d < 0 else mt.ORDER_TYPE_SELL
    mt.positions.append(
        NS(
            symbol=NAME,
            magic=desk.h.magic,
            type=side,
            ticket=72,
            time_msc=(T - 10) * 1000,
            volume=0.01,
            price_open=2000,
            sl=0,
            tp=0,
            profit=0,
        )
    )
    assert desk.owners.bind(mt.positions[0], 1)
    r = FirstCrossGate().apply(candidate(d, T, Structure(T, d, 2 * d)), observe=True)
    dispatch(desk, NAME, [r], data_ok=True, history_ok=True, rewarmed=False)
    assert not mt.positions and [x.get("position") for x in mt.requests] == [72]
    assert not any(e == "batch_preview" for e, _ in log)
    assert [o["outcome"] for e, o in log if e == "label_decision"] == [
        "first_trend_cross_after_mss"
    ]


def test_rejected_quote_snapshot_never_spends_first_or_swallows_later_raw_exit():
    mt = MT()
    lane = Stream(1, Indicator(), 0.01)
    lane.baseline_due = False
    v = Structure(T, 1, 2)
    lane.project = lambda *args: candidate(1, T + 60, v)
    tick, other = mt.q, NS(**(vars(mt.q) | {"time_msc": mt.q.time_msc + 1}))
    it = iter([tick, other])
    mt.symbol_info_tick = lambda n: next(it)
    assert sample(mt, NAME, [lane], lambda: T + 0.2, 4) == ([], False)
    assert lane.h.gate.record == {}
    mt.symbol_info_tick = lambda n: tick
    rows, ok = sample(mt, NAME, [lane], lambda: T + 0.2, 4)
    assert ok and len(rows) == 1 and rows[0].label == 1 and rows[0].entry_side == 0
    assert sample(mt, NAME, [lane], lambda: T + 0.2, 4) == ([], True)
    lane.project = lambda *args: candidate(-1, T + 60, v)
    rows, ok = sample(mt, NAME, [lane], lambda: T + 0.2, 4)
    assert (
        ok
        and len(rows) == 1
        and rows[0].label == -1
        and rows[0].entry_reason == "opposite_signal_exit_only"
    )
