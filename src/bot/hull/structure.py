"""One causal, protected-swing state machine per symbol/timeframe. Closed OHLC only."""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite
from typing import Literal

from bot.zones.engine import Bar

ENTRY_POLICY = "protected_swing_v2"


@dataclass(frozen=True, slots=True)
class Pivot:
    price: float
    bar_s: int  # OPEN of the candle containing the wick
    known_s: int  # close at which this observation became available


@dataclass(frozen=True, slots=True)
class Band:
    name: str
    low: float | None
    high: float | None
    low_open: bool = False
    high_open: bool = False

    def contains(self, px: float) -> bool:
        # One finite edge = one-sided permission. Two missing edges are NEVER permission.
        return (
            isfinite(px)
            and (self.low is not None or self.high is not None)
            and (
                self.low is None or (px > self.low if self.low_open else px >= self.low)
            )
            and (
                self.high is None
                or (px < self.high if self.high_open else px <= self.high)
            )
        )


@dataclass(frozen=True, slots=True)
class Structure:
    """Frozen at signal close, including the entry boundary used on EVERY executable quote."""

    bias: int = 0
    event: Literal["none", "seed", "choch", "bos"] = "none"
    broken: Pivot | None = None  # previously known level broken by a strict close
    break_close_s: int | None = None
    origin: Pivot | None = None  # PROTECTED HL/LH; never replaced by a minor pullback
    extreme: Pivot | None = None  # confirmed HH/LL beyond the structural break
    # Deepest observed retracement, not the protected swing.
    pullback: Pivot | None = None
    pullback_seen_s: int | None = None  # first counter-close after this tip
    break_now: bool = False  # applies to this closed candle ONLY
    impulse_tip: Pivot | None = None  # observed wick; not a claim of pivot confirmation

    @property
    def continuation_side(self):
        """Permission already known BEFORE a forming bar (no break-close exception)."""
        return (
            self.bias
            if (
                self.event in ("choch", "bos")
                and self.origin is not None
                and self.pullback_seen_s is not None
            )
            else 0
        )

    @property
    def phase(self) -> str:
        if self.event in ("none", "seed"):
            return "unconfirmed_context"
        if self.break_now:
            return self.event + "_close"
        if self.pullback_seen_s is not None:
            return "bull_pullback" if self.bias == 1 else "bear_pullback"
        return "wait_pullback"

    def band(self, side: int) -> Band | None:
        if side not in (-1, 1) or self.event not in ("choch", "bos") or not self.origin:
            return None
        if side == self.bias:
            if self.break_now and self.event == "choch":
                if self.broken is None:
                    return None
                # The breakout must still hold at the executable quote.
                edge = self.broken.price
                return (
                    Band("bull_choch_buy", edge, None, low_open=True)
                    if side == 1
                    else Band("bear_choch_sell", None, edge, high_open=True)
                )
            if self.pullback_seen_s is None and not (
                self.break_now and self.event == "bos"
            ):
                return None
            edge = self.origin.price
            return (
                Band("bull_protected_pullback_buy", edge, None, low_open=True)
                if side == 1
                else Band("bear_protected_pullback_sell", None, edge, high_open=True)
            )
        # Separate, unchanged countertrend exception; never a blanket reversal permission.
        if self.event != "bos" or self.extreme is None or self.broken is None:
            return None
        level, tip = self.broken.price, self.extreme.price
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
        if self.event == "bos" and side == -self.bias:
            return "structure_wait_confirmed_extreme"
        return "structure_" + self.phase


class InternalStructure:
    """Local pivots locate extremes; only a structural closing break changes protection.

    Fixed memory. A running leg wick and a confirmed swing are deliberately distinct.
    The break candle cannot supply both a previous retracement and the break that proves it.
    """

    def __init__(self):
        self.view = Structure()
        self.high: Pivot | None = None
        self.low: Pivot | None = None
        self.prev_close: float | None = None
        self.tip: Pivot | None = None
        # Outer wick of the preceding leg for a genuine reversal.
        self.outer: Pivot | None = None
        self.retracement: Pivot | None = None
        self.seen_s: int | None = None

    def _start(
        self,
        side: int,
        event: Literal["seed", "choch", "bos"],
        level: Pivot,
        origin: Pivot,
        close_s: int,
    ) -> Structure:
        self.tip = self.outer = self.retracement = None
        self.seen_s = None
        return Structure(side, event, level, close_s, origin, break_now=event != "seed")

    def update(
        self, bar: Bar, close_s: int, ph: Pivot | None, pl: Pivot | None
    ) -> Structure:
        s = self.view
        if s.break_now:
            s = replace(s, break_now=False)
        if not s.bias:
            # A first break seeds context, never an order. Its guard is not trailed.
            if self.high is not None and self.low is not None:
                if bar.c > self.high.price:
                    s = self._start(1, "seed", self.high, self.low, close_s)
                elif bar.c < self.low.price:
                    s = self._start(-1, "seed", self.low, self.high, close_s)
        else:
            d, guard = s.bias, s.origin
            assert guard is not None and self.outer is not None
            if d * (bar.c - guard.price) < 0:
                # Use the outer swing that launched the reversal, NOT the last local pivot.
                s = self._start(-d, "choch", guard, self.outer, close_s)
            elif s.extreme is not None and d * (bar.c - s.extreme.price) > 0:
                if self.seen_s is not None and self.retracement is not None:
                    r = self.retracement  # known before this break candle
                    # A swept/equal old guard is never promoted as a higher low/lower high.
                    promote = (
                        d * (r.price - guard.price) > 0
                        and d * (s.extreme.price - r.price) > 0
                    )
                    s = self._start(
                        d, "bos", s.extreme, r if promote else guard, close_s
                    )
                else:
                    # Strict extension but no intervening pullback: same impulse, no BOS.
                    s = replace(s, extreme=None)
                    self.tip = self.retracement = None
                    self.seen_s = None
        if s.bias:
            d = s.bias
            w = bar.h if d == 1 else bar.l
            back = bar.l if d == 1 else bar.h
            if self.outer is None or d * (w - self.outer.price) >= 0:
                self.outer = Pivot(w, bar.t, close_s)
            if s.extreme is None and (
                self.tip is None or d * (w - self.tip.price) >= 0
            ):
                # Newest equal unconfirmed tip wins. A tip candle cannot be its own pullback.
                self.tip = Pivot(w, bar.t, close_s)
                self.seen_s = self.retracement = None
            else:
                if self.retracement is None or d * (back - self.retracement.price) <= 0:
                    self.retracement = Pivot(back, bar.t, close_s)
                if (
                    self.seen_s is None
                    and self.prev_close is not None
                    and d * (bar.c - self.prev_close) < 0
                ):
                    self.seen_s = close_s
            tip_pivot = ph if d == 1 else pl
            if (
                s.extreme is None
                and tip_pivot is not None
                and self.tip is not None
                and tip_pivot.bar_s == self.tip.bar_s
                and d * (tip_pivot.price - s.broken.price) > 0
            ):
                s = replace(s, extreme=tip_pivot)
            s = replace(
                s,
                impulse_tip=self.tip,
                pullback_seen_s=self.seen_s,
                pullback=self.retracement if self.seen_s is not None else None,
            )
        # A pivot first confirmed now was NOT already a known break level on this close.
        if ph is not None:
            self.high = ph
        if pl is not None:
            self.low = pl
        self.prev_close = bar.c
        self.view = s
        return s
