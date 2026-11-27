from types import SimpleNamespace as NS

import pytest

from bot.hull.bar import Bar
from bot.hull.orders import Block, Grid, protected_prices
from bot.hull.signal import Reading
from bot.hull.structure import Structure


def grid():
    return Grid(0.01, 0.01, 2, 0.05, 0.01, 100, 0.01)


def paired_reading(*, side, close, cross, wick_sl):
    return NS(
        entry_side=side,
        entry_reason="ready",
        side=side,
        raw_sl=wick_sl,
        paired=True,
        cross_bar=Bar(0, 100, 101, 99, close),
        cross_price=cross,
        close=close,
        stop_tick_size=None,
    )


@pytest.mark.parametrize(
    "side,bid,ask,close,cross,wick_sl,expected_raw,expected_sl",
    [
        (1, 101.00, 101.02, 100.50, 100.00, 99.00, 100.00, 100.00),
        (-1, 99.00, 99.02, 99.50, 100.00, 100.50, 100.00, 100.02),
    ],
)
def test_cross_region_tightens_wick_stop_for_both_sides(
    side, bid, ask, close, cross, wick_sl, expected_raw, expected_sl
):
    plan = protected_prices(
        paired_reading(side=side, close=close, cross=cross, wick_sl=wick_sl),
        NS(lots=0.1, max_spread=None),
        grid(),
        bid,
        ask,
    )
    assert plan[2] == expected_sl
    assert plan[4] == expected_raw
    assert plan[6] == cross
    assert plan[7] == "cross_region_tightened"


def test_farther_cross_keeps_the_more_protective_wick_stop():
    plan = protected_prices(
        paired_reading(side=1, close=100.0, cross=98.0, wick_sl=99.0),
        NS(lots=0.1, max_spread=None),
        grid(),
        101.0,
        101.02,
    )
    assert plan[4] == 99.0
    assert plan[7] == "existing_base_stop_retained"


def test_intrabar_entry_keeps_swing_stop_until_origin_candle_close():
    reading = paired_reading(side=1, close=99.9, cross=100.0, wick_sl=99.0)
    reading.intrabar = True
    reading.region = "inside"
    reading.trigger = NS(bid=100.5)
    plan = protected_prices(
        reading,
        NS(lots=0.1, max_spread=None),
        grid(),
        101.0,
        101.02,
    )
    assert plan[4] == 99.0
    assert plan[6] == 100.0
    assert plan[7] == "awaiting_origin_close"


@pytest.mark.parametrize(
    "side,close,cross,bid,ask,reason",
    [
        (1, 99.9, 100.0, 101.0, 101.02, "cross_region_close_invalidated"),
        (-1, 100.1, 100.0, 99.0, 99.02, "cross_region_close_invalidated"),
        (1, 100.5, 100.0, 99.0, 99.02, "cross_region_wrong_side_or_too_close"),
        (-1, 99.5, 100.0, 100.1, 100.12, "cross_region_wrong_side_or_too_close"),
    ],
)
def test_wrong_side_close_or_first_quote_blocks_entry(
    side, close, cross, bid, ask, reason
):
    with pytest.raises(Block, match=reason):
        protected_prices(
            paired_reading(side=side, close=close, cross=cross, wick_sl=99.0 if side == 1 else 101.0),
            NS(lots=0.1, max_spread=None),
            grid(),
            bid,
            ask,
        )


@pytest.mark.parametrize("side", [-1, 1])
def test_missing_cross_intersection_blocks_paired_entry(side):
    with pytest.raises(Block, match="cross_region_unavailable"):
        protected_prices(
            paired_reading(side=side, close=100.5, cross=None, wick_sl=99.0 if side == 1 else 101.0),
            NS(lots=0.1, max_spread=None),
            grid(),
            101.0 if side == 1 else 99.0,
            101.02 if side == 1 else 99.02,
        )


@pytest.mark.parametrize("side", [-1, 1])
def test_closed_paired_signal_vetoes_close_on_wrong_side_of_intersection(side):
    close_s = 3600
    cross = 100.0
    reading = Reading(
        close_s=close_s,
        tf=15,
        close=cross - side * 0.1,
        a=100.0,
        b=100.1,
        atr=1.0,
        label=side,
        side=side,
        reason="ready",
        anchor=99.0 if side == 1 else 101.0,
        anchor_s=close_s - 12 * 60,
        raw_sl=98.0 if side == 1 else 102.0,
        window_start_s=None,
        prior_bars=0,
        pivot_low=None,
        pivot_high=None,
        structure=Structure(closed_s=close_s - 300, bias=side),
        trigger_structure=Structure(closed_s=close_s, bias=side),
        trigger_tf=12,
        cross_bar=Bar(close_s - 12 * 60, 100.0, 101.0, 99.0, cross - side * 0.1),
        cross_price=cross,
    )
    assert reading.entry_reason == "cross_region_close_invalidated"
    assert reading.entry_side == 0
