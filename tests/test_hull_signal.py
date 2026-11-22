import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from bot.hull.bar import Bar
from bot.hull.config import Indicator
from bot.hull.signal import Hull

FIX = json.loads(
    (
        Path(__file__).resolve().parents[1] / "tests/fixtures/hull_reference.json"
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


def test_retired_either_and_ema_filters_cannot_be_applied_to_new_signals():
    for options in ({"source": "either"}, {"signal": "ema_plan"}):
        with pytest.raises(ValidationError):
            Indicator(**options)
    a = Hull(15, Indicator(), 0.01)  # supported technically, not automatically eligible
    a.update(tape(15)[-1])
    with pytest.raises(ValueError):
        a.update(tape(15)[-1])


def test_atr_seed_is_wilder_and_equal_flat_values_never_cross():
    e = Hull(15, Indicator(), 0.01, tick_size=0.01)
    ds = [e.update(c) for c in tape(15)]
    assert ds[12].atr is None and ds[13].atr is not None
    flat = Hull(1, Indicator(), 0.01)
    assert not any(
        flat.update(Bar(T + i * 60, 100, 100, 100, 100)).label for i in range(60)
    )
