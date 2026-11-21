"""Chart Hull arithmetic. Projections never mutate closed buffers."""

from __future__ import annotations
import math
from collections import deque
from dataclasses import dataclass
from itertools import chain, islice


class WMA:
    def __init__(self, n):
        self.q = deque(maxlen=n)
        self.n = n
        self.div = n * (n + 1) / 2

    def next(self, v, commit):
        if commit:
            if v is None:
                self.q.clear()
                return None
            self.q.append(v)
            if len(self.q) != self.n:
                return None
            terms = self.q
        else:
            if v is None or len(self.q) + 1 < self.n:
                return None
            # Same multiply/add order as push, without modifying the closed buffer.
            terms = chain(islice(self.q, int(len(self.q) == self.n), None), (v,))
        weighted = 0.0
        for i, x in enumerate(terms, 1):
            weighted += i * x
        return weighted / self.div


class Baseline:
    """One source's entire A history. Previous B is shared; previous A is not."""

    def __init__(self, half, length):
        self.fast, self.slow = WMA(half), WMA(length)
        self.smooth = WMA(max(1, math.floor(math.sqrt(length) + 0.5)))
        self.last = None

    def update(self, source, b, prev_b, commit):
        x, y = self.fast.next(source, commit), self.slow.next(source, commit)
        a = self.smooth.next(
            2 * x - y if x is not None and y is not None else None, commit
        )
        prev_a = self.last
        if commit:
            self.last = a
        cross = 0
        if None not in (a, b, prev_a, prev_b):
            if b > a and prev_b < prev_a:
                cross = 1
            elif b < a and prev_b > prev_a:
                cross = -1
        return a, cross


@dataclass(frozen=True, slots=True)
class LiveTick:
    ms: int
    bid: float
    ask: float

    @classmethod
    def read(cls, q):
        if q is None:
            return None
        ms = getattr(q, "time_msc", None)
        b, a = getattr(q, "bid", None), getattr(q, "ask", None)
        if not (
            isinstance(ms, int)
            and ms > 0
            and isinstance(b, (float, int))
            and isinstance(a, (float, int))
            and 0 < b <= a
            and math.isfinite(a)
        ):
            return None
        return cls(ms, b, a)
