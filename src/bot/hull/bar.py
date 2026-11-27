"""Validated bid-chart candle; t is its minute-aligned open, in Unix seconds."""

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class Bar:
    t: int  # UTC minute OPEN in Unix seconds
    o: float
    h: float
    l: float
    c: float

    def __post_init__(self):
        if (
            self.t % 60
            or not all(isfinite(x) for x in (self.o, self.h, self.l, self.c))
            or not (0 < self.l <= min(self.o, self.c) <= max(self.o, self.c) <= self.h)
        ):
            raise ValueError("Invalid bid bar or non-minute timestamp")


def cross_location(side, cross, bid, low, high):
    """Classify ONCE at the triggering quote. Wrong-side and equality never admit.

    The inclusive wick range is the entire Bid candle SO FAR, not a final OHLC
    or a back-projected fill. An already tighter stop is never widened.
    """
    if (
        side not in (-1, 1)
        or not all(
            type(v) in (int, float) and isfinite(v) and v > 0
            for v in (cross, bid, low, high)
        )
        or not low <= bid <= high
    ):
        return "unverified"
    if side * (bid - cross) <= 0:
        return "wrong_side"
    return "inside" if low <= cross <= high else "outside"
