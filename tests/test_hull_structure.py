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
    # A completed lower close after HH (higher after LL) is sufficient. The
    # counter-swing itself is not protected and needs no three-right confirmation.
    assert ds[9].side == ds[9].entry_side == bias and s.pullback_seen_s == 10 * dt
    assert s.origin.price == price(105.75) and s.pullback.price == price(114.75)
    s = ds[11].structure
    assert (s.pullback.price, s.pullback.bar_s, s.pullback.known_s) == (
        price(112.75),
        10 * dt,
        11 * dt,
    )
    assert s.band(-bias) is None  # fresh CHoCH does not provide a countertrend band
    for x in (105.76, 112.74, 112.75, 115.51, 118.25, 125):
        assert s.veto(bias, price(x)) == ""
    for x in (105.74, 105.75):
        assert s.veto(bias, price(x)).startswith("outside_")
    assert ds[13].entry_side == bias and ds[13].entry_reason == "ready"
    assert ds[12].label == 0 and ds[12].entry_side == 0
    # Equal close and wick beyond are not BOS. On the strict break the extreme
    # of the intervening pullback becomes protected, never the latest micro-pivot.
    assert ds[15].structure.origin == s.origin and ds[15].structure.extreme == s.extreme
    for r in ds[16:18]:
        assert r.structure.event == "bos" and r.structure.broken == s.extreme
        assert r.structure.origin == s.pullback and r.structure.band(-bias) is None
    assert ds[16].structure.break_now and ds[16].structure.band(bias) is not None
    assert not ds[17].structure.break_now and ds[17].structure.band(bias) is None
    q = ds[18].structure
    assert q.extreme.price == price(124.25) and q.extreme.known_s == 19 * dt
    assert q.band(-bias).contains(price(120)) and not q.band(-bias).contains(
        price(118.25)
    )
    assert q.band(-bias).contains(price(124.25)) and not q.band(-bias).contains(
        price(124.26)
    )
    assert ds[20].entry_side == -bias  # genuine countertrend cross beyond BOS
    # A minor swing is breached on 21, the actual protected swing on 22.
    assert ds[21].structure.bias == bias and ds[21].structure.origin == s.pullback
    assert ds[22].structure.event == "choch" and ds[22].structure.bias == -bias
    assert ds[22].structure.broken == s.pullback and ds[22].structure.band(bias) is None
    assert (
        ds[22].structure.band(-bias) is not None
    )  # CHoCH-close permission, not a raw cross
    assert (
        sequence(bias, tf, PATH[:14])[-1] == ds[13]
    )  # future decisions cannot change a past snapshot


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
    ds = sequence(bias, closes=PATH[:10] + [106, 108, 119])
    # Retrace wick equals the protected swing; it is evidence, not a new HL/LH.
    assert ds[-2].structure.pullback.price == ds[-2].structure.origin.price
    assert (
        ds[-1].structure.event == "bos"
        and ds[-1].structure.origin == ds[-2].structure.origin
    )
    # Without a previously known structural pivot or a counter-direction close,
    # successive new extremes do not manufacture a second BOS or a pullback.
    ds = sequence(bias, closes=PATH[:9] + [119, 124, 122, 120, 121])
    assert all(
        x.structure.event == "choch" and x.structure.band(-bias) is None for x in ds[8:]
    )
    assert ds[10].structure.extreme is None and ds[10].structure.band(bias) is None
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
