"""Prices and timestamps are specified up front; never identify a swing in hindsight."""

from dataclasses import replace

import pytest
from hull_cases import PATH, sequence

from bot.hull.orders import select_labels
from bot.hull.structure import Structure


@pytest.mark.parametrize("tf", [1, 2, 5])
@pytest.mark.parametrize("bias", [1, -1])
def test_local_sequence_protected_range_and_separate_bos_leg(tf, bias):
    ds = sequence(bias, tf)

    def price(x):
        return x if bias == 1 else 240 - x

    dt = tf * 60
    assert ds[4].structure.event == "seed" and not any(d.entry_side for d in ds[:9])
    assert ds[7].structure.event == "choch" and ds[7].structure.extreme is None
    assert ds[8].structure.band(bias) is None
    s = ds[9].structure
    assert s.extreme.price == price(118.25)
    assert (s.extreme.bar_s, s.extreme.known_s) == (8 * dt, 10 * dt)
    # Even a real Hull cross on the HH confirmation close cannot backdate the later HL.
    assert ds[9].side == bias and not ds[9].entry_side and s.pullback is None
    assert ds[10].structure.pullback is None
    s = ds[11].structure
    assert (s.pullback.price, s.pullback.bar_s, s.pullback.known_s) == (
        price(112.75),
        10 * dt,
        12 * dt,
    )
    assert s.band(-bias) is None  # a CHoCH, not a continuation BOS
    for x in (112.75, 114, 115.5):
        assert s.veto(bias, price(x)) == ""
    for x in (112.74, 115.51, 118.25):
        assert s.veto(bias, price(x)).startswith("outside_")
    assert ds[13].entry_side == bias and ds[13].entry_reason == "ready"
    assert ds[12].label == 0 and ds[12].entry_side == 0
    # Close exactly at the high (wick beyond) is NOT a break. The next actual
    # close past it retires the previous trend band, but isn't yet a post-BOS HH.
    assert ds[15].structure == s
    for row in ds[16:18]:
        assert row.structure.event == "bos" and row.structure.broken == s.extreme
        assert row.structure.origin == s.pullback and row.structure.band(-bias) is None
        assert row.structure.band(bias) is None
    q = ds[18].structure
    assert q.extreme.price == price(124.25) and q.extreme.known_s == 19 * dt
    assert q.pullback is None and q.band(-bias) is not None
    assert q.band(-bias).contains(price(120)) and not q.band(-bias).contains(
        price(118.25)
    )
    assert q.band(-bias).contains(price(124.25)) and not q.band(-bias).contains(
        price(124.26)
    )
    assert (
        ds[20].entry_side == -bias
    )  # genuine countertrend Hull label, not a synthetic signal
    # Protected HL/LH violated by a CLOSE: start opposite CHoCH, no old range carried across.
    assert ds[21].structure.event == "choch" and ds[21].structure.bias == -bias
    assert ds[21].structure.band(bias) is None and ds[21].structure.band(-bias) is None
    assert (
        sequence(bias, tf, PATH[:14])[-1] == ds[13]
    )  # prefix invariance, including decision/zone


@pytest.mark.parametrize("bias", [1, -1])
@pytest.mark.parametrize(
    "close,allowed", [(118.1, False), (118.25, False), (118.26, True)]
)
def test_real_countertrend_label_wick_only_equal_and_beyond_bos(bias, close, allowed):
    r = sequence(bias, closes=PATH[:18] + [118.35, 117.8, close])[-1]
    assert r.label == r.side == -bias and r.structure.event == "bos"
    assert (
        bias * (r.close + bias * 0.25 - r.structure.broken.price) > 0
    )  # wick always crossed
    assert r.entry_side == (-bias if allowed else 0)
    assert (r.entry_reason == "ready") == allowed


@pytest.mark.parametrize("bias", [1, -1])
def test_equal_pullback_or_new_high_before_pullback_does_not_fake_structure(bias):
    ds = sequence(bias, closes=PATH[:10] + [106, 108])
    assert ds[-1].structure.pullback is None  # equal to the origin wick, not an HL/LH
    ds = sequence(bias, closes=PATH[:10] + [119, 124, 122, 120, 121])
    assert all(
        v.structure.event == "choch" and v.structure.band(-bias) is None for v in ds[9:]
    )
    assert (
        ds[10].structure.extreme is None
    )  # new extreme, no confirmed intervening HL/LH
    assert ds[-1].structure.band(bias) is not None


def test_exit_pick_unchanged_lower_aligned_valid_entry_no_conflict_or_stale_fallback():
    good = sequence()[13]
    noise = replace(good, tf=5, structure=Structure())
    assert select_labels([good, noise]) == (noise, good, "ready")
    only_old = replace(good, close_s=good.close_s - 60)
    assert select_labels([noise, only_old]) == (
        noise,
        None,
        "structure_unconfirmed_context",
    )
    other = replace(noise, label=-1, side=-1)
    # An INVALID opposite lane still participates in the old EXIT pick, but
    # cannot veto an otherwise eligible ENTRY on an empty symbol.
    assert select_labels([good, other]) == (None, good, "ready")
    valid_other = replace(sequence(-1)[13], tf=5)
    assert select_labels([good, valid_other]) == (
        None,
        None,
        "opposite_labels_same_close",
    )
    assert select_labels([noise, other]) == (None, None, "no_structure_qualified_label")
    assert select_labels([replace(good, label=0, side=0, reason="no_cross")]) == (
        None,
        None,
        "no_executable_label",
    )
