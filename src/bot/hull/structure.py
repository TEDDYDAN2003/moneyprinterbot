"""Causal internal structure of ONE symbol/timeframe. All inputs are finished bars.

The existing Hull pivots supply events only on their confirmation close. Structure
is an entry filter, not a label generator, stop anchor or position manager.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

from bot.zones.engine import Bar

ENTRY_POLICY = "internal_structure_v1"


@dataclass(frozen=True, slots=True)
class Pivot:
    price: float
    bar_s: int  # pivot candle OPEN, not the time at which it was knowable
    known_s: int  # confirmation candle CLOSE


@dataclass(frozen=True, slots=True)
class Band:
    name: str
    low: float
    high: float
    low_open: bool = False
    high_open: bool = False

    def contains(self, px: float) -> bool:
        return (self.low < px if self.low_open else self.low <= px) and (
            px < self.high if self.high_open else px <= self.high
        )


@dataclass(frozen=True, slots=True)
class Structure:
    """Immutable snapshot carried by a label all the way to the last pre-send check."""

    bias: int = 0  # context / candidate direction, NOT sufficient to allow entries
    event: Literal["none", "seed", "choch", "bos"] = "none"
    broken: Pivot | None = None  # old swing level, NOT the break candle's high/low
    break_close_s: int | None = None
    origin: Pivot | None = None  # opposite swing at CHoCH, or prior HL/LH at BOS
    extreme: Pivot | None = None  # newly confirmed HH / LL in this impulse
    pullback: Pivot | None = None  # later confirmed HL / LH

    @property
    def phase(self) -> str:
        if self.event in ("none", "seed"):
            return "unconfirmed_context"
        if self.extreme is None:
            return "wait_hh" if self.bias == 1 else "wait_ll"
        if self.pullback is None:
            return "wait_hl" if self.bias == 1 else "wait_lh"
        return "bull_range" if self.bias == 1 else "bear_range"

    def band(self, side: int) -> Band | None:
        if (
            side not in (-1, 1)
            or self.extreme is None
            or self.event not in ("choch", "bos")
        ):
            return None
        tip = self.extreme.price
        if side == self.bias:
            if self.pullback is None:
                return None
            base = self.pullback.price
            # Preserve an exact quoted midpoint (do not fuzz strict BOS edges).
            mid = float((Decimal(str(tip)) + Decimal(str(base))) / 2)
            return (
                Band("bull_discount_buy", base, mid)
                if side == 1
                else Band("bear_premium_sell", mid, base)
            )
        if self.event != "bos" or self.broken is None:
            return None
        level = self.broken.price
        return (
            Band("sell_above_bull_bos", level, tip, low_open=True)
            if side == -1
            else Band("buy_below_bear_bos", tip, level, high_open=True)
        )

    def veto(self, side: int, px: float) -> str:
        band = self.band(side)
        if band is not None:
            return "" if band.contains(px) else "outside_" + band.name
        if self.event == "choch" and side == -self.bias:
            return "structure_needs_continuation_bos"
        return "structure_" + self.phase


class InternalStructure:
    """Track strict CLOSE breaks of known pivots, then distinct, ordered swings.

    Cold start: the first break sets context only. A break against that context
    is CHoCH; a continuation BOS requires an already completed HH/HL or LL/LH.
    There is no lookback search for trades when a later bar confirms a pivot.
    """

    def __init__(self):
        self.view = Structure()
        self.high: Pivot | None = None
        self.low: Pivot | None = None
        self.tip_price: float | None = None
        self.tip_bar: int | None = None

    def _start(
        self,
        side: int,
        event: Literal["seed", "choch", "bos"],
        level: Pivot,
        origin: Pivot,
        close_s: int,
    ) -> None:
        self.view = Structure(side, event, level, close_s, origin)
        self.tip_price = self.tip_bar = None

    def update(
        self, bar: Bar, close_s: int, ph: Pivot | None, pl: Pivot | None
    ) -> Structure:
        # 1. Break tests use only levels known BEFORE this candle closed.
        s = self.view
        if self.high is not None and self.low is not None:
            if not s.bias:
                if bar.c > self.high.price:
                    self._start(1, "seed", self.high, self.low, close_s)
                elif bar.c < self.low.price:
                    self._start(-1, "seed", self.low, self.high, close_s)
            else:
                guard = s.pullback or s.origin
                if guard is not None and s.bias * (bar.c - guard.price) < 0:
                    # A newly confirmed origin is usable now; the broken guard
                    # itself still had to be known on the preceding close.
                    base = (ph or self.high) if s.bias == 1 else (pl or self.low)
                    self._start(-s.bias, "choch", guard, base, close_s)
                elif s.extreme and s.bias * (bar.c - s.extreme.price) > 0:
                    if s.pullback is not None:
                        self._start(s.bias, "bos", s.extreme, s.pullback, close_s)
                    else:
                        # No new HL/LH yet: extend the same impulse, never relabel
                        # a CHoCH as BOS or inherit the superseded HH/LL's band.
                        self.view = replace(s, extreme=None)
                        self.tip_price = self.tip_bar = None

        # 2. Admit pivots only now. The tip must actually belong to the impulse
        # that started at the break; a pre-break pivot confirmed later is not it.
        s = self.view
        tip, reaction = (ph, pl) if s.bias == 1 else (pl, ph)
        if s.event == "seed" and reaction is not None:
            # Latest confirmed opposing swing defines the bootstrap context only.
            self.view = replace(s, origin=reaction)
        elif s.event in ("choch", "bos"):
            if s.extreme is None:
                px = bar.h if s.bias == 1 else bar.l
                if self.tip_price is None or s.bias * (px - self.tip_price) >= 0:
                    self.tip_price, self.tip_bar = px, bar.t
                if (
                    tip is not None
                    and tip.bar_s == self.tip_bar
                    and s.bias * (tip.price - s.broken.price) > 0
                ):
                    s = self.view = replace(s, extreme=tip)
            if (
                s.extreme
                and s.pullback is None
                and reaction
                and reaction.bar_s > s.extreme.bar_s
                and s.bias * (reaction.price - s.origin.price) > 0
                and s.bias * (s.extreme.price - reaction.price) > 0
            ):
                self.view = replace(s, pullback=reaction)

        if ph is not None:
            self.high = ph
        if pl is not None:
            self.low = pl
        return self.view
