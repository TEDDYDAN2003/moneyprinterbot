"""Pure M15 close clock, in integer broker ticks. No network, wall clock or orders."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import islice

from .config import Rules

TF = 900


def ticks(price, step) -> int:
    r = Decimal(str(price)) / Decimal(str(step))
    if (
        not r.is_finite()
        or r <= 0
        or abs(r - r.to_integral_value()) > Decimal("0.000001")
    ):
        raise ValueError("Nonpositive or off-grid price")
    return int(r.to_integral_value())


@dataclass(frozen=True, slots=True)
class Bar:
    t: int  # native UTC bar OPEN, seconds
    o: int
    h: int
    l: int
    c: int

    def __post_init__(self):
        if (
            self.t % TF
            or self.t < 0
            or not 0 < self.l <= min(self.o, self.c) <= max(self.o, self.c) <= self.h
        ):
            raise ValueError("Invalid M15 OHLC/time")

    def wick(self, side):
        return self.h if side == 1 else self.l


@dataclass(frozen=True, slots=True)
class Point:
    i: int
    v: int


@dataclass(slots=True)
class Swing:
    last: Point | None = None
    inner: Point | None = None  # LH for highs, HL for lows
    bos: Point | None = None
    used: bool = True
    pull: Point | None = None


class Structure:
    """Causal port of supplied f_structure(3,15), NOT Hull's BOS algorithm."""

    def __init__(self):
        self.i = -1
        self.bias = 0
        self.legs = {1: Swing(), -1: Swing()}
        self.prot = self.shift_level = None
        self.ev_i = self.shift_i = -1
        self.shift_dir = 0
        self.history = deque(maxlen=1501)  # newest first, including current

    def prior_extreme(self, side: int, n: int):
        # Start with the most recent, matching Pine's strict comparison in a backward scan.
        recent = (
            Point(self.i - k, b.wick(side))
            for k, b in enumerate(islice(self.history, 1, max(1, n + 1)), 1)
        )
        return max(recent, key=lambda p: side * p.v, default=None)

    def update(self, b: Bar) -> str:
        self.i += 1
        self.history.appendleft(b)
        win = list(islice(self.history, 7))
        for d, leg in self.legs.items():
            if len(win) != 7:
                continue
            v = win[3].wick(d)
            if any(d * (v - x.wick(d)) <= 0 for x in win[:3]) or any(
                d * (v - x.wick(d)) < 0 for x in win[4:]
            ):
                continue
            pivot = Point(self.i - 3, v)
            if leg.last and d * (v - leg.last.v) < 0:
                leg.inner = pivot
            leg.last = pivot
            if pivot.i >= self.ev_i and (
                leg.bos is None or leg.used or d * (v - leg.bos.v) > 0
            ):
                leg.bos, leg.used, leg.pull = pivot, False, self.prior_extreme(-d, 2)
        event = ""
        if self.bias and self.prot and self.bias * (b.c - self.prot.v) < 0:
            d = -self.bias
            edge = self.prior_extreme(-d, min(1500, self.i - 1 - self.prot.i))
            self.prot, self.shift_level = edge, edge or self.legs[-d].last
            self.bias = self.shift_dir = d
            self.ev_i = self.shift_i = self.i
            self.legs[d].inner = None
            for leg in self.legs.values():
                leg.bos, leg.used = None, True
            event = "mss"
        else:
            for d, leg in self.legs.items():
                if (
                    self.bias != -d
                    and leg.bos
                    and not leg.used
                    and d * (b.c - leg.bos.v) > 0
                ):
                    leg.used = True
                    other = self.legs[-d].last
                    if leg.pull:
                        self.prot = leg.pull
                    elif self.prot is None and other and d * (b.c - other.v) > 0:
                        self.prot = other
                    self.bias, self.ev_i = d, self.i
                    if self.shift_dir == d:
                        self.shift_dir = 0
                    event = "bos"
                    break
        for d, leg in self.legs.items():
            v = b.wick(-d)
            if leg.bos and (not leg.pull or d * (v - leg.pull.v) < 0):
                leg.pull = Point(self.i, v)
        return event

    def stop(self, b, d, buf):
        if self.shift_dir == d and self.i - self.shift_i <= 15:
            return self.shift_level.v - d * buf if self.shift_level else None
        leg = self.legs[-d]
        provisional = (
            self.prior_extreme(-d, min(300, self.i - 1 - leg.last.i))
            if leg.last
            else None
        )
        anchor = next(
            (
                p.v
                for p in (provisional, leg.inner, leg.last)
                if p and d * (b.c - p.v) > 0
            ),
            b.o,
        )
        return anchor - d * buf


@dataclass(frozen=True, slots=True)
class Zone:
    side: int
    born: int  # M15 opening time; stable id within this symbol
    index: int
    lo: int
    hi: int
    sl: int


@dataclass(frozen=True, slots=True)
class Signal:
    zone: Zone
    close_s: int
    entry: int  # reference rejection close

    @property
    def tp(self):
        return 2 * self.entry - self.zone.sl


@dataclass(slots=True)
class Decision:
    trend: int
    event: str
    raw: int = 0
    armed: Zone | None = None
    signal: Signal | None = None
    ended: list[tuple[int, str]] = field(default_factory=list)


def verdict(z: Zone, b: Bar, age: int, trend: int, ttl: int):
    d = z.side
    if trend != d:
        return "trend_changed"
    if b.c < z.lo if d == 1 else b.c > z.hi:
        return "disrespected"
    if d * (b.wick(-d) - z.sl) <= 0:
        return "stop_touched"
    if 1 <= age <= ttl and b.h >= z.lo and b.l <= z.hi and d * (b.c - b.o) > 0:
        if 2 * b.c - z.sl <= 0:
            return "invalid_target"
        return "respected"
    return "expired" if age >= ttl else ""


class Engine:
    def __init__(self, rules: Rules, zone_ticks: int):
        if zone_ticks < 0:
            raise ValueError("Negative zone width")
        self.rules, self.width = rules, zone_ticks
        self.structure = Structure()
        self.pending: list[Zone] = []
        self.last: int | None = None

    def update(self, b: Bar) -> Decision:
        if self.last is not None and b.t <= self.last:
            raise ValueError("Duplicate/unordered M15 close")
        gap = self.last is not None and b.t != self.last + TF
        self.last = b.t
        s, r = self.structure, self.rules
        event = s.update(b)
        out = Decision(s.bias, event)
        # Newest respected origin wins a collision; consume every matured candidate.
        keep = []
        for z in reversed(self.pending):
            why = (
                "data_gap"
                if gap
                else verdict(z, b, s.i - z.index, s.bias, r.retest_bars)
            )
            if why == "respected":
                if out.signal is None:
                    out.signal = Signal(z, b.t + TF, b.c)
                else:
                    why = "same_close_newer_setup"
            if why:
                out.ended.append((z.born, why))
            else:
                keep.append(z)
        self.pending = keep[::-1]
        d = (b.c > b.o) - (b.c < b.o)
        if (
            d
            and abs(b.c - b.o) >= r.min_body_ticks
            and d * (b.o - b.wick(-d)) <= r.tolerance_ticks
        ):
            out.raw = d
            if s.bias == d:
                sl = s.stop(b, d, r.stop_buffer_ticks)
                edge = b.o + d * self.width
                if sl is not None and 0 < sl and d * (edge - sl) > 0:
                    base = b.wick(-d)
                    out.armed = Zone(d, b.t, s.i, min(edge, base), max(edge, base), sl)
                    self.pending.append(out.armed)
        return out
