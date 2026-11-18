"""Causal availability and state transitions, with fixtures separate from market data."""

from math import sin

import pytest

from bot.config.loader import load_yaml
from bot.zones.config import ZoneConfig
from bot.zones.engine import Bar, Frame, Pool, ZoneEngine

Z = ZoneConfig.model_validate(load_yaml("config.yaml")["zones"])
S = Z.symbols["XAUUSD"]


def primed(role=1, tf=1):
    e = ZoneEngine(Z.rules.model_copy(update={"max_chase_atr": 10}), S)
    for i in range(210):
        p = 100 + role * 0.26
        e.update(Bar(i * 60, p, p + 0.12, p - 0.12, p))
    z = Pool(
        "fixture",
        tf,
        100,
        0.1,
        role,
        210 * 60 - 60,
        100000,
        "HL" if role == 1 else "LH",
    )
    e.zones = [z]
    e.frames[tf].latest_key[role] = z.key
    return e


def candle(index, role, o, h, l, c):
    return (
        Bar(index * 60, o, h, l, c)
        if role == 1
        else Bar(index * 60, 200 - o, 200 - l, 200 - h, 200 - c)
    )


@pytest.mark.parametrize("role", [1, -1])
@pytest.mark.parametrize(
    ("lo", "reason"), [(99.67, "sweep_reclaim"), (99.98, "respected_pullback")]
)
def test_zone_reaction_is_directional_and_one_shot(role, lo, reason):
    e = primed(role)
    d = e.update(candle(210, role, 100.01, 100.36, lo, 100.33))
    assert (d.side, d.reason, d.tf) == (role, reason, 1)
    assert d.pool == ("sell_side" if role == 1 else "buy_side")
    assert d.observed_levels and d.momentum * role > 0
    assert not e.update(candle(211, role, 100.01, 100.36, lo, 100.33)).side


@pytest.mark.parametrize("role", [1, -1])
def test_bos_needs_later_pullback_and_momentum(role):
    e = primed(-role)
    d = e.update(candle(210, role, 99.90, 100.32, 99.80, 100.29))
    assert d.side == 0 and (1, role) in d.breaks and d.reason == "BOS_wait_retest"
    # Bar fully above broken resistance cannot be a retest, regardless of momentum.
    assert not e.update(candle(211, role, 100.45, 100.85, 100.40, 100.82)).side
    d = e.update(candle(212, role, 100.13, 100.41, 100.01, 100.38))
    assert (d.reason, d.side) == ("bos_retest", role)


def test_higher_timeframe_intrabar_breach_is_not_bos():
    e = primed(1, 5)
    d = e.update(Bar(210 * 60, 100.25, 100.28, 99.65, 99.72))
    assert not d.side and not d.breaks and "outside_wait_source_close" in d.reason
    d = e.update(Bar(211 * 60, 99.72, 100.42, 99.70, 100.37))
    assert (d.side, d.tf, d.reason) == (1, 5, "sweep_reclaim")
    assert d.invalidation == pytest.approx(99.65)


def test_failed_break_and_retest_expiration_do_not_generate_entry():
    e = primed(-1)
    b = Bar(210 * 60, 99.79, 100.39, 99.75, 100.30)
    e.update(b)
    d = e.update(Bar(211 * 60, 100.3, 100.4, 99.60, 99.65))
    assert not d.side and d.reason == "retest_invalidated"
    assert all(z.key != "fixture" for z in e.zones)
    e = primed(-1)
    e.update(b)
    e.r = e.r.model_copy(update={"retest_minutes": 1})
    e.update(Bar(211 * 60, 100.5, 100.58, 100.44, 100.55))
    d = e.update(Bar(212 * 60, 100.11, 100.42, 99.99, 100.4))
    assert d.zone_id != "fixture"


def test_confirmed_pivot_not_known_at_extreme_and_missing_minutes_are_not_ohlc():
    e = primed()
    e.zones.clear()
    for i, h in enumerate([100.5, 101.5, 103.0, 101.3, 100.6]):
        d = e.update(Bar((210 + i) * 60, 100.3, h, 100.2, 100.3))
        if i < 4:
            assert not any(z.price == 103 for z in e.zones)
    z = next(z for z in e.zones if z.price == 103)
    assert z.born == 215 * 60 and d.zone_id != z.key
    f = Frame(5, 2)
    out = [f.ingest(Bar(i * 60, 1, 2, 0.5, 1.5)) for i in [0, 1, 3, 4, 5, 6, 7, 8, 9]]
    assert all(b is None for b in out[:-1]) and out[-1].t == 300


def test_incremental_decisions_are_prefix_invariant_and_gaps_rewarm():
    bars = []
    for i in range(500):
        o = 200 + 2 * sin(i / 4)
        c = 200 + 2 * sin((i + 1) / 4)
        bars.append(Bar(i * 60, o, max(o, c) + 0.1, min(o, c) - 0.1, c))

    def run(stream):
        e = ZoneEngine(Z.rules, S)
        return [e.update(b) for b in stream]

    assert (
        run(bars[:400])
        == run(bars[:400] + [Bar(b.t, 1000, 1100, 900, 1050) for b in bars[400:]])[:400]
    )
    e = primed()
    assert e.update(Bar(800 * 60, 100, 101, 99, 100)).reason == "warming_up"
    with pytest.raises(ValueError, match="strictly increasing"):
        e.update(Bar(800 * 60, 100, 101, 99, 100))
