"""Shared test data, never used to select live parameters."""

from bot.hull.config import Indicator
from bot.hull.signal import Hull
from bot.hull.structure import Pivot, Structure
from bot.zones.engine import Bar


def known_range(side, price, stamp):
    """A confirmed range around `price` for desk tests not about pivot discovery."""

    def pivot(offset, age):
        return Pivot(price + side * offset, stamp - (age + 2) * 60, stamp - age * 60)

    return Structure(
        side,
        "choch",
        pivot(-0.5, 12),
        stamp - 9 * 60,
        pivot(-3, 11),
        pivot(3, 5),
        pivot(-1, 2),
    )


# 7: bullish CHoCH, 8→9: HH confirmed, 10→11: HL confirmed. 15 touches the old
# high by close / exceeds it by wick; 16 actually breaks it. 17→18: post-BOS HH.
PATH = [
    110,
    115,
    112,
    114,
    108,
    111,
    106,
    112,
    118,
    115,
    113,
    114,
    116,
    114.5,
    116,
    118.25,
    119,
    124,
    122,
    120.9,
    121.8,
    120.5,
    112,
]


def sequence(bias=1, tf=1, closes=PATH, end_s=None):
    start = (end_s - len(closes) * 60 * tf) if end_s is not None else 0
    pipe = Hull(
        tf, Indicator(swing_len=1, hull_len=2, source="close", atr_length=2), 0.01
    )
    out = []
    for i, x in enumerate(closes):
        v = x if bias == 1 else 240 - x
        out.append(pipe.update(Bar(start + 60 * tf * i, v, v + 0.25, v - 0.25, v)))
    return out
