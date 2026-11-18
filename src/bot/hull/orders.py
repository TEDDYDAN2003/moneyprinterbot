"""Price native protection on the broker tick grid. Cash conversion is injected."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .config import COUNTS


class Block(ValueError):
    """Known reason to skip, never a reason to resize or remove a stop."""


@dataclass(frozen=True, slots=True)
class Grid:
    tick: float
    point: float
    digits: int
    gap: float
    vmin: float
    vmax: float
    vstep: float

    @classmethod
    def from_info(cls, i):
        if i is None:
            raise Block("missing_symbol_spec")
        o = cls(
            float(i.trade_tick_size),
            float(i.point),
            int(i.digits),
            float(max(i.trade_stops_level, i.trade_freeze_level)) * i.point,
            float(i.volume_min),
            float(i.volume_max),
            float(i.volume_step),
        )
        if (
            not all(
                math.isfinite(v)
                for v in (o.tick, o.point, o.gap, o.vmin, o.vmax, o.vstep)
            )
            or min(o.tick, o.point, o.vmin, o.vstep) <= 0
            or o.vmax < o.vmin
            or o.gap < 0
            or not 0 <= o.digits <= 12
        ):
            raise Block("invalid_symbol_spec")
        return o

    def volume(self, lots):
        if (
            not math.isfinite(lots)
            or not self.vmin - 1e-10 <= lots <= self.vmax + 1e-10
            or abs(
                (lots - self.vmin) / self.vstep - round((lots - self.vmin) / self.vstep)
            )
            > 1e-7
        ):
            raise Block("illegal_lot_no_resize")

    def index(self, price, up):
        return int(
            (Decimal(str(price)) / Decimal(str(self.tick))).to_integral_value(
                rounding=ROUND_CEILING if up else ROUND_FLOOR
            )
        )

    def price(self, index):
        return round(index * self.tick, self.digits)


@dataclass(frozen=True, slots=True)
class Plan:
    side: int
    lots: float
    entry: float
    sl: float
    tp: float
    risk_usd: float
    reward_usd: float


def valid_cash(fn, side, lots, entry, exit):
    x = fn(side, lots, entry, exit)
    if x is None or not math.isfinite(x):
        raise Block("cash_conversion_unavailable")
    return float(x)


def target(grid, side, lots, entry, usd, cash):
    # Smallest favorable tradable tick that reaches the USD goal, including FX/CFDs.
    start = grid.index(entry, side == -1)
    lo = 0
    hi = 1
    limit = start - 1 if side == -1 else 2**40
    if limit < 1:
        raise Block("unreachable_cash_target")

    def worth(steps):
        return valid_cash(cash, side, lots, entry, grid.price(start + side * steps))

    while worth(hi) + 1e-9 < usd:
        if hi >= limit:
            raise Block("unreachable_cash_target")
        lo, hi = hi, min(2 * hi, limit)
    while hi - lo > 1:
        k = (hi + lo) // 2
        if worth(k) + 1e-9 >= usd:
            hi = k
        else:
            lo = k
    return grid.price(start + side * hi), worth(hi)


def make_plan(d, p, grid, bid, ask, cash):
    if not d.side or d.raw_sl is None:
        raise Block(d.reason)
    if not all(math.isfinite(v) for v in (bid, ask, d.raw_sl)) or not 0 < bid <= ask:
        raise Block("bad_quote")
    if p.max_spread is not None and ask - bid > p.max_spread + grid.tick * 1e-7:
        raise Block("spread_cap")
    grid.volume(p.lots)
    side = d.side
    entry, mark = (ask, bid) if side == 1 else (bid, ask)
    # Use the actual chart-derived price; never replace with a cash stop or add a hidden spread buffer.
    sl = grid.price(grid.index(d.raw_sl, side == -1))
    dist = max(grid.gap, grid.tick)
    if sl <= 0 or side * (mark - sl) < dist - grid.tick * 1e-6:
        raise Block("swing_breached_or_too_close")
    risk = -valid_cash(cash, side, p.lots, entry, sl)
    if risk <= 0:
        raise Block("nonpositive_cash_risk")
    tp, reward = target(grid, side, p.lots, entry, p.target_usd, cash)
    if side * (tp - mark) < dist - grid.tick * 1e-6:
        raise Block("cash_target_inside_broker_distance")
    return Plan(side, p.lots, entry, sl, tp, risk, reward)


@dataclass(frozen=True, slots=True)
class Exposure:
    count: int = 0
    lots: float = 0.0
    risk: float = 0.0


def room(lim, e, p, n):
    if e.count + n > lim.max_positions:
        return "portfolio_position_limit"
    if e.lots + n * p.lots > lim.max_total_lots + 1e-8:
        return "portfolio_lot_limit"
    if e.risk + n * p.risk_usd > lim.max_open_risk_usd + 1e-7:
        return "portfolio_risk_limit"
    return ""


def count_for(rate, lim, e, p, more=None):
    for n in COUNTS[rate]:
        why = room(lim, e, p, n) or (more(n) if more is not None else "")
        if not why:
            return n
    raise Block(why)


def choose(ds):
    """One non-overlapping batch per symbol; never multiply a shared timestamp."""
    ready = [d for d in ds if d.side]
    if not ready:
        return None, "no_executable_label"
    t = max(d.close_s for d in ready)
    same = [d for d in ready if d.close_s == t]
    if len({d.side for d in same}) > 1:
        return None, "opposite_labels_same_close"
    return max(same, key=lambda d: d.tf), "ready"
