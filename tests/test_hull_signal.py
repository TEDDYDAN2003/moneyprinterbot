import json
from dataclasses import replace
from pathlib import Path

import pytest

from bot.hull.config import Indicator
from bot.hull.orders import choose
from bot.hull.signal import Hull
from bot.zones.engine import Bar

FIX = json.loads(
    (
        Path(__file__).resolve().parents[1] / "research/hull_labels/reference.json"
    ).read_text()
)
T = 1800000000


def tape(tf=1):
    return [
        Bar(T + i * 60 * tf, d["open"], d["high"], d["low"], d["close"])
        for i, d in enumerate(FIX["bars"][:-1])
    ]


@pytest.mark.parametrize("ix", [0, 1, 2, 3])
def test_literal_javascript_reference_all_closed_rows(ix):
    ref = FIX["sets"][ix]
    p = Indicator(hull_len=ref["len"], source=ref["source"])
    engine = Hull(1, p, 0.01)
    for c, row in zip(tape(), ref["rows"], strict=True):
        d = engine.update(c)
        for a, b in zip(
            (d.a, d.b, d.label, d.atr, d.pivot_low, d.pivot_high), row, strict=True
        ):
            if b is None:
                assert a is None
            else:
                assert a == pytest.approx(b, abs=2e-12, rel=0)
    assert any(v[2] == 1 for v in ref["rows"]) and any(v[2] == -1 for v in ref["rows"])


@pytest.mark.parametrize("tf", [1, 2, 5])
def test_pullback_uses_previous_counterleg_local_extreme_not_future_or_signal(tf):
    p = Indicator()
    eng = Hull(tf, p, 0.01)
    bars = tape(tf)
    last = {1: None, -1: None}
    seen = set()
    for j, c in enumerate(bars):
        d = eng.update(c)
        if not d.label:
            continue
        span = bars[max(0, j - p.pullback_bars, last[-d.label] or 0) : j]
        lo = d.label == 1
        best = min(x.l for x in span) if lo else max(x.h for x in span)
        assert d.anchor == best and d.anchor_s == max(
            x.t for x in span if (x.l if lo else x.h) == best
        )
        assert (
            d.anchor_s < c.t
            and d.window_start_s == span[0].t
            and d.raw_sl == pytest.approx(best - d.label * 0.1 * d.atr)
        )
        last[d.label] = j
        seen.add(d.label)
    assert seen == {1, -1}


def test_raw_label_and_ema_plan_different_by_design_no_alias_or_m15():
    a, b = (Hull(1, Indicator(signal=mode), 0.01) for mode in ("labels", "ema_plan"))
    v = [(a.update(c), b.update(c)) for c in tape()]
    assert all(x.label == y.label for x, y in v)
    assert any(x.side and y.reason == "ema_filter" for x, y in v)
    d = next(x for x, _ in v if x.side)
    assert choose([d, replace(d, tf=5)])[0].tf == 5
    assert (
        choose([d, replace(d, tf=5, side=-d.side)])[1] == "opposite_labels_same_close"
    )
    assert choose([replace(d, side=0), d])[0] == d
    with pytest.raises(ValueError):
        Hull(15, Indicator(), 0.01)
    with pytest.raises(ValueError):
        a.update(tape()[-1])


def test_exact_pivot_mode_is_optional_and_atr_sma_seed_is_wilder():
    p = Indicator(stop="confirmed_pivot")
    e = Hull(1, p, 0.01)
    ds = [e.update(c) for c in tape()]
    assert ds[12].atr is None and ds[13].atr is not None
    assert all(
        x.anchor == (x.pivot_low if x.label == 1 else x.pivot_high)
        for x in ds
        if x.label
    )
    flat = Hull(1, Indicator(), 0.01)
    assert not any(
        flat.update(Bar(T + i * 60, 100, 100, 100, 100)).label for i in range(60)
    )
