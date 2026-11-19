"""Protected-swing v2 acceptance. Synthetic OHLC, never broker history or real orders."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from bot.hull.config import Indicator
from bot.hull.signal import Hull
from bot.hull.structure import Band
from bot.zones.engine import Bar

FIX = json.loads(
    (Path(__file__).resolve().parents[1] / "MT5/qa/protected_cases.json").read_text()
)["fixtures"]


def prices(d, v):
    return v if d == -1 else round(7360 - v, 3)


def bars(d, tf=1):
    data = FIX["protected-sell" if d == -1 else "protected-buy"]
    return [Bar(data[0][0] + i * tf * 60, *row[1:]) for i, row in enumerate(data)]


def tape(d, tf=1, stop=None):
    h = Hull(tf, Indicator(), 0.01)
    return [h.update(b) for b in bars(d, tf)[:stop]]


@pytest.mark.parametrize("d", [-1, 1])
@pytest.mark.parametrize("tf", [1, 2, 5])
def test_minor_swing_at_3675945_is_not_a_reversal_on_the_fresh_cross(d, tf):
    data = tape(d, tf)
    micro, major = (prices(d, v) for v in (3675.945, 3682.84))
    for i in (77, 89, 90, 91, 92):
        assert data[i].structure.origin.price == major
        assert data[i].structure.event == "bos" and data[i].structure.bias == d
    assert data[77].structure.pullback.price == micro
    assert (
        d * (data[89].close - micro) < 0
    )  # close THROUGH the minor wick, but not the origin
    r = data[92]
    assert r.close == prices(d, 3677.159) and r.label == r.side == r.entry_side == d
    assert r.entry_reason == "ready" and r.entry_audit()["protected_level"] == major
    assert r.structure.pullback.bar_s == bars(d, tf)[91].t
    assert (
        r.close_s < r.structure.pullback.bar_s + 4 * 60 * tf
    )  # pivot turn not yet 3-right confirmed
    assert tape(d, tf, 93)[-1] == r  # right edge: NO later LL/HH is in this input
    assert d * (r.close - r.structure.extreme.price) < 0
    # Confirming a later BOS promotes the deepest wick, not a more recent minor bounce.
    assert all(v.structure.origin.price == major for v in data[92:107])
    b = data[107].structure
    assert b.event == "bos" and b.break_now and b.broken == r.structure.extreme
    assert b.origin == r.structure.pullback and b.origin.price == prices(d, 3679.83)
    assert data[113].structure.extreme.price == prices(d, 3667.96)
    assert d * (data[113].structure.extreme.price - b.broken.price) > 0


@pytest.mark.parametrize("d", [-1, 1])
def test_true_choch_agreeing_cross_only_on_break_close_and_on_correct_side_of_quote(d):
    ds = tape(d)
    r = ds[45]
    s = r.structure
    edge = prices(d, 3682.76)
    assert s.event == "choch" and s.break_now and s.pullback_seen_s is None
    assert s.broken == ds[44].structure.origin and s.broken.price == edge
    assert r.label == r.entry_side == d and s.band(d).contains(r.close)
    assert not s.band(d).contains(edge) and not s.band(d).contains(edge - d * 0.001)
    assert s.band(d).contains(edge + d * 0.001)
    assert ds[46].structure.band(d) is None  # not a permanent breakout bypass
    assert (
        s.band(-d) is None and r.entry_audit()["entry_policy"] == "protected_swing_v2"
    )


@pytest.mark.parametrize("d", [-1, 1])
def test_one_completed_move_after_tip_no_three_right_bars_no_doji_or_same_bar(d):
    b = bars(d)
    h = Hull(1, Indicator(), 0.01)
    for x in b[:46]:
        h.update(x)

    def row(i, o, hi, lo, c):
        p = lambda v: prices(d, v)
        return Bar(b[i].t, p(o), max(p(hi), p(lo)), min(p(hi), p(lo)), p(c))

    # Although 46 closes up in the sell case, it also sets a new low; ordering inside
    # that candle is unknowable. Equal close on 47 is not a completed move back.
    r1 = h.update(row(46, 3681.12, 3682, 3679, 3681.50))
    r2 = h.update(row(47, 3681.50, 3682, 3680.4, 3681.50))
    for r in (r1, r2):
        assert r.structure.band(d) is None and r.structure.pullback_seen_s is None
    r3 = h.update(row(48, 3681.50, 3683, 3680.6, 3682))
    assert r3.structure.extreme is None and r3.structure.pullback_seen_s == r3.close_s
    assert r3.structure.band(d).contains(r3.close)
    r4 = h.update(row(49, 3682, 3682.50, 3678.6, 3679))
    assert (
        r4.structure.pullback_seen_s is None
        and r4.structure.origin == r3.structure.origin
    )
    assert r3.structure.band(d).contains(r3.close)  # immutable close, no backdating


@pytest.mark.parametrize("d", [-1, 1])
@pytest.mark.parametrize("delta", [-0.001, 0, 0.001])
def test_protected_guard_wick_equal_and_one_tick_through(d, delta):
    b = bars(d)
    h = Hull(1, Indicator(), 0.01)
    for x in b[:92]:
        prev = h.update(x)
    edge = prev.structure.origin.price
    c = round(edge + d * delta, 3)
    x = replace(b[92], h=max(edge + 0.2, b[92].h), l=min(edge - 0.2, b[92].l), c=c)
    s = h.update(x).structure
    if delta < 0:
        assert s.bias == -d and s.event == "choch" and s.broken == prev.structure.origin
    else:
        assert s.bias == d and s.origin == prev.structure.origin and not s.break_now
        assert s.band(d).contains(c) == (delta > 0)


@pytest.mark.parametrize("d", [-1, 1])
def test_no_promotion_on_wick_or_equal_close_no_guard_widen_after_sweep(d):
    b = bars(d)
    h = Hull(1, Indicator(), 0.01)
    for x in b[:92]:
        prev = h.update(x)
    s = prev.structure
    guard = s.origin.price
    old = s.extreme.price

    def candle(i, c, through_guard=False):
        hi = max(
            c, b[i].o, old + 0.5, guard + 0.1 if through_guard and d == -1 else old
        )
        lo = min(c, b[i].o, old - 0.5, guard - 0.1 if through_guard and d == 1 else old)
        return Bar(b[i].t, b[i].o, hi, lo, c)

    equal = h.update(candle(92, old, True)).structure
    assert equal.bias == d and equal.origin == s.origin and not equal.break_now
    assert (
        d * (equal.pullback.price - guard) < 0
    )  # wick-only sweep, not a lower high / higher low
    new = h.update(candle(93, round(old + d * 0.001, 3))).structure
    assert (
        new.break_now
        and new.event == "bos"
        and new.broken == s.extreme
        and new.origin == s.origin
    )


def test_one_sided_boundaries_are_strict_finite_and_fail_closed_when_absent():
    for z in (Band("buy", 100, None, True), Band("sell", None, 100, high_open=True)):
        assert all(
            not z.contains(x) for x in (100, float("inf"), float("-inf"), float("nan"))
        )
    assert not Band("absent", None, None).contains(100)
    assert Band("buy", 100, None, True).contains(200) and Band(
        "sell", None, 100, high_open=True
    ).contains(50)


@pytest.mark.parametrize("d", [-1, 1])
@pytest.mark.parametrize("target", [45, 92])
@pytest.mark.parametrize("touch", [False, True])
def test_incident_and_choch_go_through_real_shadow_desk_with_frozen_quote_boundary(
    hull_desk, d, target, touch
):
    from hull_mt import T

    mt, desk, _ = hull_desk(armed=False)
    bs = bars(d)[: target + 1]
    offset = T - 60 - bs[-1].t
    engine = Hull(1, Indicator(), 0.01)
    for b in bs:
        r = engine.update(replace(b, t=b.t + offset))
    assert r.close_s == T and r.entry_side == d
    band = r.structure.band(d)
    edge = band.low if d == 1 else band.high
    px = edge if touch else r.close
    mt.q.bid, mt.q.ask = (
        (round(px - 0.01, 3), px) if d == 1 else (px, round(px + 0.01, 3))
    )
    expected = "quote_outside_" + band.name if touch else "shadow_plan_only"
    assert desk.batch("XAUUSDm", r) == expected
    assert not mt.requests and not mt.positions


def test_long_tapes_cannot_flip_bias_or_promote_origins_on_internal_noise():
    import random

    rng = random.Random(271828)
    breaks = {("choch", -1): 0, ("choch", 1): 0, ("bos", -1): 0, ("bos", 1): 0}
    for _ in range(12):
        h = Hull(1, Indicator(), 0.01)
        tape_rows = []
        prev = h.structure.view
        price = 10000
        for i in range(500):
            o = price
            price += rng.randint(-12, 12)
            b = Bar(
                i * 60,
                o,
                max(o, price) + rng.randint(0, 8),
                min(o, price) - rng.randint(0, 8),
                price,
            )
            s = h.update(b).structure
            if prev.bias and s.bias != prev.bias:
                assert s.break_now and s.event == "choch" and s.broken == prev.origin
                assert prev.bias * (b.c - prev.origin.price) < 0
            if s.break_now and s.event == "bos":
                assert s.bias == prev.bias and s.broken == prev.extreme
                assert (
                    s.bias * (b.c - s.broken.price) > 0
                    and prev.pullback_seen_s is not None
                )
                # Independent scan of the actual previous bars (NOT the incremental candidate).
                span = tape_rows[prev.extreme.bar_s // 60 + 1 :]
                assert span
                back = min(z.l for z in span) if s.bias == 1 else max(z.h for z in span)
                if (
                    s.bias * (back - prev.origin.price) > 0
                    and s.bias * (prev.extreme.price - back) > 0
                ):
                    assert s.origin.price == back
                else:
                    assert s.origin == prev.origin
            if prev.bias and s.origin != prev.origin:
                assert s.break_now and s.event in ("choch", "bos")
            if s.extreme is not None:
                assert (
                    s.bias * (s.extreme.price - s.broken.price) > 0
                    and s.extreme.known_s <= (i + 1) * 60
                )
            if s.break_now:
                breaks[(s.event, s.bias)] += 1
            tape_rows.append(b)
            prev = s
    assert min(breaks.values()) > 10
