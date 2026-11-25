"""Account-independent cash ladder. Prices are on the broker grid, from the actual fill.

The initial stop/TP are live until a replacement PAIR is accepted. Never use a
candle extreme, aggregate batch P&L, or a target extended by an earlier rung as
input to the next threshold. This module has no terminal or order side effects.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from .config import FILL_BREAKEVEN, PULLBACK_HALF
from .orders import Block, target, valid_cash


@dataclass(frozen=True, slots=True)
class Protection:
    rung: int  # zero = no percentage step yet; TP is the original target
    sl: float
    tp: float


def percentages(rung):
    if rung < 1:
        raise ValueError("Ladder rungs start at one")
    trigger = 70 + 30 * (rung - 1)
    return trigger, trigger - 10, trigger + 50


def cash_levels(base, rung):
    trigger, lock, take = percentages(rung)
    return {
        "rung": rung,
        "trigger_usd": base * (trigger / 100),
        "lock_usd": base * (lock / 100),
        "target_usd": base * (take / 100),
    }


def reached(profit, base):
    if not math.isfinite(base) or base <= 0 or not math.isfinite(profit):
        raise Block("ladder_invalid_cash")
    units = (profit / base) * 10
    if not math.isfinite(units):
        raise Block("ladder_invalid_cash")
    # A tiny dimensionless epsilon absorbs binary price arithmetic at exact rungs.
    return max(0, 1 + math.floor((units - 7 + 1e-9) / 3))


def executable(side, bid, ask):
    if side not in (1, -1) or not (
        math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask
    ):
        raise Block("ladder_invalid_quote")
    return bid if side == 1 else ask


def inside(side, mark, pair):
    return side * (mark - pair.sl) > 0 and side * (pair.tp - mark) > 0


def _legal_levels(grid, side, mark, old, new):
    gap, tol = max(grid.gap, grid.tick), grid.tick * 1e-6
    return (
        inside(side, mark, old)
        and min(new.sl, new.tp) > 0
        and side * (new.sl - old.sl) >= 0
        and side * (new.tp - old.tp) >= 0
        and side * (mark - new.sl) >= gap - tol
        and side * (new.tp - mark) >= gap - tol
        and (new.sl, new.tp) != (old.sl, old.tp)
    )


def legal(grid, side, mark, old, new):
    return new.rung > old.rung and _legal_levels(grid, side, mark, old, new)


@dataclass(frozen=True, slots=True)
class EarlyStop:
    """A frozen policy and basis, supplied only when the ticket's quote history permits it."""

    policy: str
    initial_sl: float | None = None

    def candidate(self, grid, side, entry, old, mark):
        if (
            old.rung != 0
            or side not in (-1, 1)
            or not math.isfinite(entry)
            or entry <= 0
        ):
            return None
        move = side * (mark - entry)
        if self.policy == PULLBACK_HALF:
            origin = self.initial_sl
            if (
                move > 0
                or type(origin) not in (int, float)
                or not math.isfinite(origin)
                or origin <= 0
                or side * (entry - origin) <= 0
            ):
                return None
            # Use the *original broker SL*, never the current SL or the signal/limit price.
            basis = (Decimal(str(entry)) + Decimal(str(origin))) / 2
        elif self.policy == FILL_BREAKEVEN and move > 0:
            basis = Decimal(str(entry))
        else:
            return None
        # Round toward protection; never round in the direction of a larger loss.
        sl = grid.price(grid.index(basis, up=side == 1))
        if (
            side * (Decimal(str(sl)) - basis) < 0
            or side * (sl - old.sl) <= grid.tick * 1e-6
        ):
            return None
        new = Protection(0, sl, old.tp)
        return new if _legal_levels(grid, side, mark, old, new) else None


def propose(grid, side, lots, entry, base, old, mark, cash, *, early=None):
    """Prefer the standard rung. A pre-ladder amendment never extends TP."""
    if not inside(side, mark, old):
        return None
    profit = valid_cash(cash, side, lots, entry, mark)
    rung = reached(profit, base)
    if rung <= old.rung:
        return early.candidate(grid, side, entry, old, mark) if early else None
    _, lock_pct, tp_pct = percentages(rung)
    slope = profit / abs(mark - entry) if mark != entry else None
    sl, _ = target(
        grid, side, lots, entry, base * (lock_pct / 100), cash, cash_per_price=slope
    )
    tp, _ = target(
        grid, side, lots, entry, base * (tp_pct / 100), cash, cash_per_price=slope
    )
    # Even if conversion changes, never give back a stronger confirmed price.
    better = max if side == 1 else min
    new = Protection(rung, better(sl, old.sl), better(tp, old.tp))
    if not legal(grid, side, mark, old, new):
        if (
            early
            and (floor := early.candidate(grid, side, entry, old, mark)) is not None
        ):
            return floor
        raise Block("ladder_stops_or_freeze_distance")
    return new


def still_valid(grid, side, lots, entry, base, old, new, mark, cash, *, early=None):
    """Revalidate the exact request. Never send a different pair on an old precheck."""
    if new.rung == 0:
        # If a full rung became executable during precheck, discard this weaker unsent request.
        return (
            early is not None
            and propose(grid, side, lots, entry, base, old, mark, cash, early=early)
            == new
        )
    _, lock_pct, tp_pct = percentages(new.rung)
    return (
        legal(grid, side, mark, old, new)
        and reached(valid_cash(cash, side, lots, entry, mark), base) >= new.rung
        and valid_cash(cash, side, lots, entry, new.sl) + 1e-9
        >= base * (lock_pct / 100)
        and valid_cash(cash, side, lots, entry, new.tp) + 1e-9 >= base * (tp_pct / 100)
    )
