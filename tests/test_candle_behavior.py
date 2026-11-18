from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest

from bot.config.loader import load_yaml
from bot.zones.candle_replay import OpenTrade, benchmark, exit_event
from bot.zones.candles import patterns
from bot.zones.config import ZoneConfig
from bot.zones.engine import Bar, Decision, ZoneEngine
from bot.zones.feed import Minute
from bot.zones.orders import Plan
from bot.zones.price_action import (
    adverse,
    arm,
    evidence,
    protect,
    return_plan,
    rules_for,
)

CFG = load_yaml("config.yaml")
Z = ZoneConfig.model_validate(CFG["zones"])
S = Z.symbols["XAUUSD"]
T = int(datetime(2026, 11, 3, 12, tzinfo=UTC).timestamp())


def dec(side=-1):
    return Decision(
        T,
        "sweep_reclaim",
        side,
        5,
        "pool",
        4000.0,
        0.2,
        4000.60 if side == -1 else 3999.40,
        3995.0 if side == -1 else 4005.0,
        1.5,
        momentum=0.7 * side,
        bar_direction=side,
        candle=(("bear_engulf" if side == -1 else "bull_engulf"),),
        trigger_high=4000.60,
        trigger_low=3999.40,
    )


def policy(name):
    return Z.price_action.model_copy(update={"policy": name})


def minute(t, ms, b, a=None):
    b = np.asarray(b, float)
    a = b + 0.24 if a is None else np.asarray(a, float)
    return Minute(
        Bar(t, b[0], b.max(), b.min(), b[-1]),
        np.array(ms, dtype="int64") + t * 1000,
        b,
        a,
        True,
        len(b),
    )


def test_candle_patterns_multi_label_and_mirror():
    a = Bar(T, 100.6, 100.8, 99.9, 100.0)
    b = Bar(T + 60, 99.98, 100.9, 99.85, 100.75)
    assert "bull_engulf" in patterns(b, a, 0.001)
    mirror = lambda r: Bar(r.t, 201 - r.o, 201 - r.l, 201 - r.h, 201 - r.c)
    assert "bear_engulf" in patterns(mirror(b), mirror(a), 0.001)
    hammer = Bar(T, 100.35, 100.56, 99.65, 100.53)
    assert "bull_rejection" in patterns(hammer, None, 0.001)
    assert "bear_rejection" in patterns(mirror(hammer), None, 0.001)
    assert set(patterns(Bar(T + 60, 100.36, 100.50, 100.2, 100.36), hammer, 0.001)) == {
        "inside",
        "doji",
    }
    assert patterns(Bar(T, 100, 100, 100, 100), None, 0.001) == ("flat",)


def test_prior_pattern_alone_is_not_an_entry():
    d = replace(
        dec(),
        candle=("bear_ordinary",),
        previous_candle=("bear_engulf",),
        prior_at_zone=False,
    )
    assert arm(d, S, policy("pattern_wick"))[0] is None
    assert evidence(replace(d, prior_at_zone=True)) == "previous_pattern_follow_through"
    assert (
        evidence(replace(d, prior_at_zone=True, bar_direction=1))
        == "no_directional_pattern"
    )
    assert (
        evidence(replace(d, prior_at_zone=True, candle=("bull_engulf",)))
        == "opposing_candle"
    )
    assert arm(replace(d, side=0), S, policy("return_wick"))[0] is None


@pytest.mark.parametrize("side", [1, -1])
def test_sweep_pocket_stop_is_beyond_wick_executable_and_capped(side):
    d = dec(side)
    pa = policy("reaction_managed")
    r = rules_for(Z.rules, pa.policy)
    w, _ = arm(d, S, pa)
    bid = 4000 + (-side) * 0.53
    p, why = return_plan(w, S, r, bid, bid + 0.24, T * 1000 + 1500)
    assert (
        why == "ready" and 0 < p.structural_risk_usd <= 5 and p.sl == p.structural_stop
    )
    expected = 3999.399 if side == 1 else 4000.841
    assert p.sl == pytest.approx(expected)  # short adds spread, long uses bid stop
    assert p.modeled_fill == pytest.approx(bid + (0.24 if side == 1 else 0))
    assert (
        return_plan(w, S, r, 4000, 4000.24, T * 1000 + 1500)[1]
        == "outside_swept_pocket"
    )
    assert return_plan(w, S, r, bid, bid + 0.24, w.until_ms)[1] == "outside_watch_time"
    assert return_plan(w, S, r, bid, bid + 0.35, T * 1000 + 1500)[1] == "spread_limit"
    assert (
        return_plan(w, S, r, bid, bid + 0.24, T * 1000 + 1500, 0.5)[1]
        == "wick_stop_too_close"
    )
    assert (
        return_plan(
            w, S, r, 4000 - side * 0.22, 4000 - side * 0.22 + 0.24, T * 1000 + 1
        )[1]
        == "wick_risk_over_budget"
    )
    # A multi-bar deeper excursion must not be clipped to the shallow last candle.
    w2, _ = arm(replace(d, invalidation=d.invalidation - side * 0.5), S, pa)
    assert side * (w2.invalid_bid - w.invalid_bid) < 0


def test_watch_invalidated_between_poll_samples_and_no_retroactive_fill(monkeypatch):
    d = dec()
    monkeypatch.setattr(
        ZoneEngine,
        "update",
        lambda self, b: d if b.t + 60 == T else Decision(b.t + 60, "away"),
    )

    def go(bids, offsets):
        stream = [minute(T - 60, [1], [4000]), minute(T, offsets, bids)]
        return benchmark(
            stream, CFG, "XAUUSD", [("w", T, T + 60)], ("sweep_wick",), paired=False
        )["w"]["arms"]["sweep_wick"]

    # Valid pockets after an earlier microtick invalidation are NOT fillable.
    x = go([3999.7, 4000.65, 4000.54, 4000.53], [1, 100, 200, 2100])
    assert x["metrics"]["trades"] == 0
    assert x["events"]["watch:invalidated_before_entry"] == 1
    # Pocket in between samples cannot be claimed at that historical price.
    x = go([3999.7, 4000.53, 3999.72], [1, 100, 2100])
    assert x["metrics"]["trades"] == 0
    # Opening price already passed the objective -> cancel, even if a fill looks good later.
    x = go([3994.9, 4000.53], [1, 2100])
    assert x["events"]["watch:objective_before_entry"] == 1
    # Pre-fill highs/lows never score a target/stop on the new position.
    x = go([3998.7, 4000.53, 4000.58], [1, 2100, 2150])
    assert x["exits"] == {"end_of_sample": 1}


def test_ticks_with_equal_ms_are_ordered_and_software_giveback_never_widens_sl():
    d = dec(1)
    p = Plan(1, 4000.0, 4000.0, 3999.5, 4006.0, 0.0, True)
    pa = policy("reaction_managed")
    m = minute(T, [0, 2000, 4000], [4000, 4001.5, 4000.72])
    tr = OpenTrade(d, p, m.ms[0], 4.5, 1)
    assert (
        exit_event(tr, m, 0, S, pa, np.array([0, 1, 2]), "barrier", True)[2]
        == "profit_giveback"
    )  # +13.50 -> +6.48
    assert tr.peak == pytest.approx(13.5) and tr.plan.sl == 3999.5
    tr.peak = 0.0  # independent new trade for equal-ms barrier ordering
    m = minute(T, [0, 0, 0], [4000, 3999, 4007])
    assert exit_event(tr, m, 0, S, pa, np.array([0]), "barrier", True)[2] == "stop"
    assert protect(2.0, 1.0, S, pa) == (2.0, False)
    assert (
        adverse(replace(d, candle=("bear_engulf",), momentum=-0.70), 1, pa)
        == "adverse_candle"
    )
    assert (
        adverse(replace(d, momentum=-0.7, prior_momentum=-0.31), 1, pa)
        == "two_adverse_closes"
    )
    assert not adverse(replace(d, momentum=-0.2, prior_momentum=-0.8), 1, pa)


def test_uncapped_diagnostic_not_a_live_plan_and_not_given_future_price(monkeypatch):
    d = replace(dec(1), level=4000.0, width=0.25, trigger_low=3996, invalidation=3996.0)
    monkeypatch.setattr(
        ZoneEngine,
        "update",
        lambda self, b: d if b.t + 60 == T else Decision(b.t + 60, "away"),
    )
    m = [
        minute(T - 60, [0], [4000]),
        minute(T, [1, 40, 2001], [4000.1, 3999.3, 4001.7]),
    ]
    x = benchmark(m, CFG, "XAUUSD", [("w", T, T + 60)], ("baseline", "return_wick"))[
        "w"
    ]
    assert (
        x["arms"]["baseline"]["exits"] == {"stop": 1}
        and x["arms"]["return_wick"]["metrics"]["trades"] == 0
    )
    assert x["unbounded_same_entries_diagnostic"]["over_budget"] == 1
    assert x["unbounded_same_entries_diagnostic"]["metrics"]["wins"] == 1
    assert "max_drawdown" not in x["unbounded_same_entries_diagnostic"]["metrics"]
