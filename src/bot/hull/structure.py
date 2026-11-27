"""Closed native-bar translation of f_structure in FC-Hull-Dynamic-Source.pine.

A guard is a reversal threshold, never a price band for entries. Bar timestamps
are in seconds in MT5; Pine uses milliseconds. Compare actual bar opens (not
computed pivot timestamps), including at market/session gaps.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from bot.hull.bar import Bar


@dataclass(frozen=True, slots=True)
class Pivot:
    price: float
    bar_s: int
    known_s: int


@dataclass(frozen=True, slots=True)
class Structure:
    closed_s: int | None = None
    bias: int = 0
    event_code: int = 0  # signed 3: INIT, 1: BOS, 2: MSS
    broken: Pivot | None = None
    guard: Pivot | None = None
    ph: Pivot | None = None
    hi_class: int = 0
    replace_hi: bool = False
    pl: Pivot | None = None
    lo_class: int = 0
    replace_lo: bool = False
    last_mss_s: int | None = None
    # Direction last established by a confirmed INIT/continuation BOS. An MSS
    # changes bias immediately but does not by itself establish follow-through.
    established_bias: int = 0
    failed_counter_mss_s: int | None = (
        None  # origin of counter-leg rejected by this epoch's MSS
    )

    # Last classed HL in this bullish range / LH in this bearish range. Never a forming wick.
    range_swing: Pivot | None = None

    @property
    def epoch(self):
        return self.closed_s if abs(self.event_code) == 2 else self.last_mss_s

    @property
    def failed_return(self) -> bool:
        return (
            self.bias in (-1, 1)
            and self.established_bias == self.bias
            and self.failed_counter_mss_s is not None
            and self.epoch is not None
            and self.closed_s is not None
            and self.failed_counter_mss_s < self.epoch <= self.closed_s
        )

    @property
    def phase(self):
        return {1: "bullish", 0: "neutral", -1: "bearish"}[self.bias]


class InternalStructure:
    def __init__(self, depth):
        self.depth = depth
        self.swing_floor = 0
        self.history = deque(maxlen=2 * depth + 1)
        self.view = Structure()
        self.hi = self.lo = self.last_h = self.last_l = None
        self.pull_l = self.pull_h = self.guard = self.leg_tip = None
        self.last_break = self.leg_start = self.leg_origin = self.break_window = None
        self.has_hi = self.has_lo = self.has_pull_h = self.has_pull_l = False

    def update(self, bar: Bar, closed: int) -> Structure:
        if self.history and bar.t <= self.history[-1].t:
            raise ValueError("Structure requires unique, ordered native closed bars")
        self.history.append(bar)
        span = list(self.history)
        pt = span[-1 - self.depth].t if len(span) > self.depth else None
        ph = pl = None
        if len(span) == self.history.maxlen:
            s = self.depth
            mid, left, right = span[s], span[:s], span[s + 1 :]
            # Pine confirms the rightmost of equal pivot extrema.
            if all(mid.h >= q.h for q in left) and all(mid.h > q.h for q in right):
                ph = Pivot(mid.h, pt, closed)
            if all(mid.l <= q.l for q in left) and all(mid.l < q.l for q in right):
                pl = Pivot(mid.l, pt, closed)

        d = self.view.bias
        ev, level = 0, None
        if d and self.guard and d * (bar.c - self.guard.price) < 0:
            ev, level, self.guard = -2 * d, self.guard, self.leg_tip
        elif (
            d >= 0
            and self.hi
            and bar.c > self.hi.price
            and (d == 1 or (self.last_l and self.last_l.price < bar.c))
        ):
            ev, level = (3 if not d else 1), self.hi
            if not d or (
                self.pull_l and (not self.guard or self.pull_l.price > self.guard.price)
            ):
                self.guard = self.pull_l or self.last_l
        elif (
            d <= 0
            and self.lo
            and bar.c < self.lo.price
            and (d == -1 or (self.last_h and self.last_h.price > bar.c))
        ):
            ev, level = (-3 if not d else -1), self.lo
            if not d or (
                self.pull_h and (not self.guard or self.pull_h.price < self.guard.price)
            ):
                self.guard = self.pull_h or self.last_h

        if ev:
            d = 1 if ev > 0 else -1
            self.last_break, self.leg_start = level.price, bar.t
            self.leg_origin, self.break_window = level.bar_s, pt
            self.hi = self.lo = self.pull_l = self.pull_h = None
            self.has_hi = self.has_lo = self.has_pull_h = self.has_pull_l = False
            self.leg_tip = Pivot(bar.h if d == 1 else bar.l, bar.t, closed)
        elif d:
            x = bar.h if d == 1 else bar.l
            if not self.leg_tip or d * (x - self.leg_tip.price) >= 0:
                self.leg_tip = Pivot(x, bar.t, closed)

        hc = lc = 0
        rh = rl = False
        in_leg = self.leg_start is None or (pt is not None and pt >= self.leg_start)
        in_window = (
            self.break_window is not None
            and pt is not None
            and pt >= self.break_window
            and (self.leg_origin is None or pt > self.leg_origin)
        )
        # Order matters on a bar that confirms both a high and a low.
        if ph:
            self.last_h = ph
            if (
                d > 0
                and self.pull_l
                and pt > self.pull_l.bar_s
                and (not self.guard or self.pull_l.price > self.guard.price)
            ):
                self.guard = self.pull_l
            beyond = self.last_break is None or ph.price > self.last_break
            across_break = (
                d > 0
                and not self.hi
                and self.last_break is not None
                and beyond
                and in_window
            )
            if (
                (in_leg or across_break)
                and d >= 0
                and beyond
                and (not self.hi or ph.price >= self.hi.price)
            ):
                hc, rh = (3 if not d else 1), self.has_hi
                self.has_hi, self.hi = True, ph
                self.pull_l, self.has_pull_l = None, False
            if (
                in_leg
                and d <= 0
                and self.lo
                and pt > self.lo.bar_s
                and (not self.pull_h or ph.price >= self.pull_h.price)
            ):
                if d < 0:
                    hc, rh = -1, self.has_pull_h
                    self.has_pull_h, self.has_lo = True, False
                self.pull_h = ph
        if pl:
            self.last_l = pl
            if (
                d < 0
                and self.pull_h
                and pt > self.pull_h.bar_s
                and (not self.guard or self.pull_h.price < self.guard.price)
            ):
                self.guard = self.pull_h
            beyond = self.last_break is None or pl.price < self.last_break
            across_break = (
                d < 0
                and not self.lo
                and self.last_break is not None
                and beyond
                and in_window
            )
            if (
                (in_leg or across_break)
                and d <= 0
                and beyond
                and (not self.lo or pl.price <= self.lo.price)
            ):
                lc, rl = (3 if not d else -1), self.has_lo
                self.has_lo, self.lo = True, pl
                self.pull_h, self.has_pull_h = None, False
            if (
                in_leg
                and d >= 0
                and self.hi
                and pt > self.hi.bar_s
                and (not self.pull_l or pl.price <= self.pull_l.price)
            ):
                if d > 0:
                    lc, rl = 1, self.has_pull_l
                    self.has_pull_l, self.has_hi = True, False
                self.pull_l = pl
        # Classify the *return* close, never predict that the first MSS will fail.
        # No candle counter and no dependence on Hull signals, fills or sessions.
        prior = self.view
        contiguous = prior.closed_s == bar.t
        established = prior.established_bias if contiguous else 0
        failed = prior.failed_counter_mss_s if contiguous else None
        if abs(ev) == 2:
            # A BOS in the intervening direction would already have promoted it
            # to established. Repeated unconfirmed flips cannot free both sides.
            failed = prior.epoch if established == d and prior.bias == -d else None
        elif abs(ev) in (1, 3):
            established = d
        swing = prior.range_swing
        if not contiguous or prior.bias != d:
            swing, self.swing_floor = None, bar.t
        # The classifier above keeps the lowest confirmed pullback low, or the
        # highest confirmed pullback high, until the next directional swing.
        pivot = pl if d == 1 and lc == 1 else ph if d == -1 and hc == -1 else None
        if pivot and pivot.bar_s >= self.swing_floor:
            swing = pivot
        if swing and d * (bar.c - swing.price) <= 0:
            swing = None  # broken support/resistance is not a new stop anchor
        self.view = Structure(
            closed,
            d,
            ev,
            level,
            self.guard,
            ph,
            hc,
            rh,
            pl,
            lc,
            rl,
            closed if abs(ev) == 2 else prior.epoch,
            established,
            failed,
            swing,
        )
        return self.view
