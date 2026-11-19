"""Either v2 signal and first-while-flat regressions. Synthetic data and fake MT5 only."""

from __future__ import annotations

import itertools
import json
import math
from dataclasses import replace
from types import SimpleNamespace as NS

import numpy as np
import pytest
from hull_mt import ROOT, T, sig

from bot.config.loader import load_yaml
from bot.hull.config import BATCH_ENTRY_POLICY, LADDER, HullConfig, Indicator
from bot.hull.orders import Grid
from bot.hull.replay import Account
from bot.hull.signal import Hull
from bot.hull.terminal import dispatch
from bot.zones.engine import Bar
from bot.zones.feed import Minute
from bot.zones.hours import Hours

NAME = "XAUUSDm"
OK = {"data_ok": True, "history_ok": True, "rewarmed": False}


def tape(tf):
    rows = json.loads((ROOT / "research/hull_labels/reference.json").read_text())[
        "bars"
    ][:-1]
    values = [[r[k] for k in ("open", "high", "low", "close")] for r in rows]
    # Shrinking ranges with unchanged closes force BOTH source directions on a bar.
    for i in range(145):
        w = 6 if i % 42 < 24 else 0.0625
        values.append([100, 100 + w, 100 - w, 100])
    # High and Low become identical; one crossover must not produce two labels.
    for i in range(115):
        px = 100 + round(2 * math.sin(i * 0.27), 4)
        values.append([px, px, px, px])
    return [Bar(T + i * 60 * tf, *r) for i, r in enumerate(values)]


def wma(v, n):
    # Independent batch oracle; use plain multiply/add order for strict equality.
    result = []
    for i in range(len(v)):
        terms = v[max(0, i + 1 - n) : i + 1]
        value = 0.0
        if len(terms) != n or None in terms:
            result.append(None)
            continue
        for weight, x in enumerate(terms, 1):
            value += weight * x
        result.append(value / (n * (n + 1) / 2))
    return result


def reference(bars):
    def dif(lists, fn):
        return [None if None in r else fn(*r) for r in zip(*lists, strict=True)]

    def a(v):
        return wma(dif((wma(v, 8), wma(v, 16)), lambda x, y: 2 * x - y), 4)

    c = [r.c for r in bars]
    b = wma(dif((wma(c, 2), wma(c, 4), wma(c, 8)), lambda x, y, z: 3 * x - y - z), 8)

    def crosses(base):
        result = [0]
        for i in range(1, len(b)):
            if None in (b[i - 1], b[i], base[i - 1], base[i]):
                result.append(0)
            else:
                result.append(
                    int(b[i] > base[i] and b[i - 1] < base[i - 1])
                    - int(b[i] < base[i] and b[i - 1] > base[i - 1])
                )
        return result

    lo = a([x.l for x in bars])
    hi = a([x.h for x in bars])
    return lo, hi, b, crosses(lo), crosses(hi)


@pytest.mark.parametrize("tf", [1, 2, 5])
def test_either_matches_independent_whole_series_strict_crosses_and_same_source_previous(
    tf,
):
    bars = tape(tf)
    h = Hull(tf, Indicator(), 0.01)
    lo, hi, b, lc, hc = reference(bars)
    rows = [h.update(v) for v in bars]
    seen = []
    last_emit = {1: None, -1: None}
    for i, r in enumerate(rows):
        assert (r.a_low, r.a_high, r.b, r.cross_low, r.cross_high) == (
            lo[i],
            hi[i],
            b[i],
            lc[i],
            hc[i],
        )
        assert r.a is None and r.close_s == bars[i].t + 60 * tf
        directions = {v for v in (lc[i], hc[i]) if v}
        expected = next(iter(directions)) if len(directions) == 1 else 0
        assert r.label == expected and r.source_conflict == (len(directions) == 2)
        if r.source_conflict:
            assert r.entry_reason == "source_conflict" and not r.side
        if expected:
            # The local-wick algorithm is unchanged, with its opposite boundary taken from
            # the selected Either stream, not from only Low/High or disagreed candidates.
            limit = last_emit[-expected]
            prior = bars[max(0, i - 7, 0 if limit is None else limit) : i]
            wick = min(v.l for v in prior) if expected == 1 else max(v.h for v in prior)
            assert r.anchor == wick and r.raw_sl == pytest.approx(
                wick - expected * 0.1 * r.atr
            )
            last_emit[expected] = i
            seen.append(expected)
    pairs = set(zip(lc, hc, strict=True))
    assert {(1, 1), (-1, -1), (0, 1), (-1, 0)} <= pairs
    assert any(r.source_conflict for r in rows) and any(
        a == b for a, b in itertools.pairwise(seen)
    )


@pytest.mark.parametrize("side", [-1, 1])
def test_live_one_remaining_leg_consumes_repeat_then_same_zone_rearms_only_on_fresh_cross(
    hull_desk, monkeypatch, side
):
    mt, d, logs = hull_desk("slow")
    r = sig(sl=1999.95 if side == 1 else 2000.55, side=side)
    now = [T + 0.3]
    monkeypatch.setattr("bot.hull.desk.time.time", lambda: now[0])
    dispatch(d, NAME, [r], **OK)
    assert len(mt.positions) == len(mt.requests) == 10
    # Nine exits are not a flat batch. No modify, exit, wait, new order or top-up.
    mt.positions[:] = mt.positions[-1:]
    survivor = mt.positions[0]
    levels = (survivor.sl, survivor.tp)
    again = replace(r, close_s=T + 60)
    now[0] += 60
    mt.q.time_msc = (T + 60) * 1000 - 10
    dispatch(d, NAME, [again], **OK)
    assert logs[-1][1]["outcome"] == "same_side_batch_open"
    assert (
        mt.positions == [survivor]
        and (survivor.sl, survivor.tp) == levels
        and len(mt.requests) == 10
        and not d.waiting
    )
    mt.positions.clear()
    mt.q.time_msc += 110
    dispatch(d, NAME, [replace(again, tf=2)], **OK)
    assert logs[-1][1]["outcome"] == "already_consumed" and not mt.positions
    now[0] += 60
    mt.q.time_msc += 60000
    dispatch(d, NAME, [replace(r, close_s=T + 120)], **OK)
    assert (
        logs[-1][1]["outcome"] == "demo_batch_sent"
        and len(mt.positions) == 10
        and len(mt.requests) == 20
    )


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize(
    "offset,entry,deal_side,expected",
    [
        (-1, 1, "closing", "shadow_plan_only"),
        (0, 1, "closing", "same_side_batch_at_signal_close"),
        (150, 1, "closing", "same_side_batch_at_signal_close"),
        (150, 3, "closing", "same_side_batch_at_signal_close"),
        (150, 2, "closing", "same_side_batch_at_signal_close"),
        (150, 0, "closing", "shadow_plan_only"),
        (150, 1, "reverse", "shadow_plan_only"),
    ],
)
def test_broker_exit_between_cross_and_poll_not_resurrected_or_confused_with_reversal(
    hull_desk, side, offset, entry, deal_side, expected
):
    mt, d, _ = hull_desk(armed=False)
    close_type = mt.DEAL_TYPE_SELL if side == 1 else mt.DEAL_TYPE_BUY
    mt.deals = [
        NS(
            symbol=NAME,
            entry=entry,
            type=close_type if deal_side == "closing" else 1 - close_type,
            magic=0,
            time_msc=T * 1000 + offset,
        )
    ]
    r = sig(1999.95 if side == 1 else 2000.55, side=side)
    assert d.batch(NAME, r) == expected and not mt.requests
    mt.deals = []
    assert d.batch(NAME, r) == "already_consumed"


@pytest.mark.parametrize("bad", [None, "missing_time"])
def test_flat_history_uncertain_is_hard_veto_not_a_queue(hull_desk, bad):
    mt, d, _ = hull_desk()
    mt.deals = (
        None
        if bad is None
        else [NS(symbol=NAME, entry=1, type=mt.DEAL_TYPE_SELL, time_msc=0)]
    )
    assert (
        d.batch(NAME, sig()) == "entry_deal_history_unavailable"
        and not d.waiting
        and not mt.requests
    )


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("when", ["before_close", "on_first_tick", "still_open"])
def test_ordered_replay_flat_at_cross_not_flat_after_processing_later_tick(side, when):
    cfg = load_yaml(ROOT / "config.yaml")
    h = HullConfig.model_validate(cfg["hull"])
    p = h.symbols["XAUUSD"].model_copy(update={"lots": 0.1, "rate": "medium"})
    a = Account(
        p,
        h.limits,
        Grid(0.001, 0.001, 3, 0, 0.01, 100, 0.01),
        lambda d, l, e, x: d * l * 100 * (x - e),
        Hours(cfg),
        10000,
        exit_opposite=True,
    )
    r = sig(sl=1999 if side == 1 else 2001, side=side)

    def minute(t, prices):
        bid = np.array(prices, dtype=float)
        return Minute(
            Bar(t, bid[0], max(bid), min(bid), bid[-1]),
            np.arange(len(bid)) + t * 1000 + 100,
            bid,
            bid + 0.1,
            True,
        )

    old = 2000.0 + side * 2
    a.step(minute(T, [2000.0, old] if when == "before_close" else [2000.0]), [r])
    second = replace(r, close_s=T + 60)
    a.step(
        minute(
            T + 60,
            [old, 2000.0]
            if when == "on_first_tick"
            else [2000.0, old]
            if when == "still_open"
            else [2000.0],
        ),
        [second],
    )
    if when == "before_close":
        assert a.count["opened"] == 2 and a.pos
    else:
        assert (
            not a.pos
            and a.count["opened"] == 1
            and a.count[
                "same_side_batch_at_signal_close"
                if when == "on_first_tick"
                else "same_side_batch_open"
            ]
            == 1
        )
        a.step(minute(T + 120, [2000.0]), [replace(r, close_s=T + 120)])
        assert a.pos and a.count["opened"] == 2
        assert a.rows[0]["exit_reason"] == "target" and a.rows[0]["legs"] == 4


def test_versioned_defaults_only_indicator_input_changes_and_legacy_opt_in():
    h = HullConfig.model_validate(load_yaml(ROOT / "config.yaml")["hull"])
    p = h.indicator
    assert p == Indicator() and (p.hull_len, p.source, p.signal_policy) == (
        16,
        "either",
        "fc_hull_either_v2",
    )
    assert (
        p.exit_opposite,
        p.stop,
        p.pullback_bars,
        p.atr_length,
        p.atr_buffer,
        p.extra_pips,
        h.exit_policy,
    ) == (True, "local_pullback", 7, 14, 0.1, 0, LADDER)
    assert BATCH_ENTRY_POLICY == "first_eligible_while_flat_v1"
    old = Indicator(hull_len=24, source="hl2")
    assert old.signal_policy == "fc_hull_single_v1"


def test_a_waiting_quote_cannot_revive_a_repeat_when_history_catches_up(hull_desk):
    from bot.hull.entry_clock import WAITING

    mt, desk, _ = hull_desk()
    quote = mt.q
    mt.q = None
    assert desk.batch(NAME, sig()) == WAITING
    mt.deals = [
        NS(symbol=NAME, entry=1, type=mt.DEAL_TYPE_SELL, time_msc=T * 1000 + 50)
    ]
    mt.q = quote
    assert desk.resume_first_quote(NAME) == "same_side_batch_at_signal_close"
    assert not desk.waiting and not mt.requests
    assert desk.batch(NAME, sig()) == "already_consumed"


def test_other_symbols_closing_or_deposit_deals_do_not_lock_this_first_entry(hull_desk):
    mt, desk, _ = hull_desk(armed=False)
    mt.deals = [
        NS(symbol="GBPJPYm", entry=1, type=mt.DEAL_TYPE_SELL, time_msc=T * 1000 + 50),
        NS(symbol="", entry=0, type=2, time_msc=T * 1000 + 80),
    ]
    assert desk.batch(NAME, sig()) == "shadow_plan_only" and not mt.requests
