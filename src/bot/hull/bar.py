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
