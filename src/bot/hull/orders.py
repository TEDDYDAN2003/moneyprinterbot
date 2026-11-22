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
    reward_usd: float  # modeled profit at the tick-rounded TP, not the ladder base
    configured_target_usd: float
    entry_spread_usd: float

    @property
    def goal_usd(self):
        return self.configured_target_usd + self.entry_spread_usd


def valid_cash(fn, side, lots, entry, exit):
    x = fn(side, lots, entry, exit)
    if isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x):
        raise Block("cash_conversion_unavailable")
    return float(x)


def spread_goal(side, lots, bid, ask, base, cash):
    """Add one explicit cushion, valued as an immediate round-trip loss on this quote.

    order_calc_profit already prices executable fills. This *additional* profit goal
    is not a fee and must never be deducted again from broker P/L. Some brokers
    legitimately round a very small spread loss to zero in account currency.
    """
    if side not in (-1, 1) or not (math.isfinite(ask) and 0 < bid <= ask):
        raise Block("bad_quote")
    entry, mark = (ask, bid) if side == 1 else (bid, ask)
    pnl = valid_cash(cash, side, lots, entry, mark) if bid != ask else 0.0
    if pnl > 0:
        raise Block("invalid_spread_valuation")
    spread = abs(pnl)
    goal = base + spread
    if not (base > 0 and math.isfinite(goal)):
        raise Block("invalid_cash_target")
    return spread, goal


def target(grid, side, lots, entry, usd, cash, *, cash_per_price=None):
    """First favorable tradable tick reaching the goal. No cached prices across solves.

    A slope from an already-priced stop or TP is ONLY a hint. Verify the bracket
    with broker-valued P/L, including the preceding tick; use bounded search for
    nonlinear FX, asymmetric tick values, or coarse currency rounding.
    """
    if not (usd > 0 and math.isfinite(usd)):
        raise Block("invalid_cash_target")
    start = grid.index(entry, side == -1)
    limit = start - 1 if side == -1 else 2**40
    if limit < 1:
        raise Block("unreachable_cash_target")
    values = {}

    def enough(steps):
        if steps not in values:
            price = grid.price(start + side * steps)
            values[steps] = valid_cash(cash, side, lots, entry, price)
        return values[steps] + 1e-9 >= usd

    hi, lo = 1, 0
    if cash_per_price and math.isfinite(cash_per_price) and cash_per_price > 0:
        guess = (usd / cash_per_price + side * (entry - grid.price(start))) / grid.tick
        if math.isfinite(guess):
            hi = min(limit, max(1, round(guess)))
    if enough(hi):
        if hi > 1:
            if not enough(hi - 1):
                lo = hi - 1
            else:
                hi -= 1
    else:
        # Try the next tick first (the linear estimate can round down by one).
        lo, hi = hi, min(limit, hi + 1)
        while not enough(hi):
            if hi >= limit:
                raise Block("unreachable_cash_target")
            lo, hi = hi, min(2 * hi, limit)
    while hi - lo > 1:
        k = (hi + lo) // 2
        if enough(k):
            hi = k
        else:
            lo = k
    return grid.price(start + side * hi), values[hi]


def cross_candle_stop(anchor, side, pad, tick_size):
    """Freeze a legal price beyond the *completed* bid candle. A pip/point is not a trade tick."""
    if (
        side not in (-1, 1)
        or tick_size is None
        or not math.isfinite(tick_size)
        or tick_size <= 0
        or not math.isfinite(anchor)
        or anchor <= 0
        or not math.isfinite(pad)
        or pad < 0
    ):
        return None
    tick = Decimal(str(tick_size))
    level = Decimal(str(anchor)) - side * max(tick, Decimal(str(pad)))
    # Wilder ATR and its multiplier arrive as floats: e.g. 3 * .1 can read
    # .30000000000000004. Do not invent an extra tick at a numeric tie.
    units = level / tick
    nearest = units.to_integral_value()
    if abs(units - nearest) <= Decimal("8.881784197001252e-16") * max(1, abs(units)):
        units = nearest
    outward = ROUND_FLOOR if side == 1 else ROUND_CEILING
    one_out = (Decimal(str(anchor)) / tick - side).to_integral_value(rounding=outward)
    units = (min if side == 1 else max)(
        one_out, units.to_integral_value(rounding=outward)
    )
    return float(units * tick)


def protected_prices(d, p, grid, bid, ask):
    """Validate signal, spread, volume and initial stop on THIS quote."""
    if not d.entry_side or d.raw_sl is None:
        raise Block(d.entry_reason)
    if not all(math.isfinite(v) for v in (bid, ask, d.raw_sl)) or not 0 < bid <= ask:
        raise Block("bad_quote")
    if p.max_spread is not None and ask - bid > p.max_spread + grid.tick * 1e-7:
        raise Block("spread_cap")
    grid.volume(p.lots)
    side = d.side
    entry, mark = (ask, bid) if side == 1 else (bid, ask)
    # Use the actual chart-derived price; never replace with a cash stop or add a hidden spread buffer.
    sl = grid.price(grid.index(d.raw_sl, side == -1))
    # The archived, standalone v4 replay has no frozen trade-grid field.
    if getattr(d, "stop_tick_size", None) is not None and (
        grid.tick != d.stop_tick_size or sl != d.raw_sl
    ):
        raise Block(
            "cross_candle_stop_grid_changed"
        )  # never re-anchor or widen a frozen SL
    dist = max(grid.gap, grid.tick)
    if sl <= 0 or side * (mark - sl) < dist - grid.tick * 1e-6:
        raise Block(
            "initial_stop_breached_or_too_close"
            if hasattr(d, "stop_tick_size")
            else "swing_breached_or_too_close"
        )
    return entry, mark, sl, dist


def make_plan(d, p, grid, bid, ask, cash):
    entry, mark, sl, dist = protected_prices(d, p, grid, bid, ask)
    side = d.side
    risk = -valid_cash(cash, side, p.lots, entry, sl)
    if risk <= 0:
        raise Block("nonpositive_cash_risk")
    spread, goal = spread_goal(side, p.lots, bid, ask, p.target_usd, cash)
    tp, reward = target(
        grid, side, p.lots, entry, goal, cash, cash_per_price=risk / abs(entry - sl)
    )
    if side * (tp - mark) < dist - grid.tick * 1e-6:
        raise Block("cash_target_inside_broker_distance")
    return Plan(side, p.lots, entry, sl, tp, risk, reward, p.target_usd, spread)


@dataclass(frozen=True, slots=True)
class Exposure:
    count: int = 0
    lots: float = 0.0
    risk: float = 0.0


def room(lim, e, p, n):
    if lim.max_positions is not None and e.count + n > lim.max_positions:
        return "portfolio_position_limit"
    if (
        lim.max_total_lots is not None
        and e.lots + n * p.lots > lim.max_total_lots + 1e-8
    ):
        return "portfolio_lot_limit"
    if (
        lim.max_open_risk_usd is not None
        and e.risk + n * p.risk_usd > lim.max_open_risk_usd + 1e-7
    ):
        return "portfolio_risk_limit"
    return ""


def count_for(rate, lim, e, p, more=None):
    for n in COUNTS[rate]:
        why = room(lim, e, p, n) or (more(n) if more is not None else "")
        if not why:
            return n
    raise Block(why)
