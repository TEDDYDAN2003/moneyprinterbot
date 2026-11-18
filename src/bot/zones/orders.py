"""Shared cash arithmetic and guards for replay and demo. All limits are entry limits.

No loss cap is guaranteed through a price gap / slippage beyond the model.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor, isfinite

from .config import Instrument, Limits, Rules
from .engine import Decision


@dataclass(frozen=True)
class Plan:
    side: int
    requested: float  # ask for BUY, bid for SELL
    modeled_fill: float  # after adverse entry slippage
    sl: float  # trigger on bid for long, ask for short
    tp: float
    cost_floor_usd: float
    clears_structure: bool
    structural_stop: float | None = None
    structural_risk_usd: float | None = None
    max_structure_lots: float | None = None  # diagnostic, NEVER changes requested size


def pnl(s: Instrument, side: int, entry: float, exit_fill: float) -> float:
    gross = side * (exit_fill - entry) * s.lots * s.contract_size
    if s.conversion == "base_usd":
        gross /= exit_fill
    return gross - s.commission * s.lots


def _trigger(s, side, entry, net):
    gain = side * (net + s.commission * s.lots) / (s.contract_size * s.lots)
    if s.conversion == "base_usd" and gain >= 1:
        raise ValueError("Cash target unavailable in quote currency")
    mark = (
        entry + gain if s.conversion == "quote_usd" else entry / (1 - gain)
    ) + side * s.slippage
    # Round BOTH barriers toward positive P&L: don't widen SL to make the order fit.
    return round(
        (
            ceil(mark / s.tick_size - 1e-9)
            if side == 1
            else floor(mark / s.tick_size + 1e-9)
        )
        * s.tick_size,
        12,
    )


def plan_entry(
    d: Decision,
    s: Instrument,
    r: Rules,
    bid: float,
    ask: float,
    min_stop_distance: float = 0.0,
) -> tuple[Plan | None, str]:
    if (
        not all(isfinite(x) for x in (bid, ask, min_stop_distance))
        or bid <= s.slippage
        or ask < bid
    ):
        return None, "invalid_quote"
    if d.side not in (-1, 1):
        return None, "no_signal"
    side, spread = d.side, ask - bid
    if spread > s.max_spread + 1e-10:
        return None, "spread_limit"
    req, mark = (ask, bid) if side == 1 else (bid, ask)
    fill = req + side * s.slippage
    if d.zone_id:
        distance = side * (bid - d.level)
        if distance < -d.width or distance > d.width + r.max_chase_atr * d.atr:
            return None, "quote_left_entry_zone"
    floor_cost = -pnl(s, side, fill, mark - side * s.slippage)
    if floor_cost >= s.sl_usd:
        return None, "costs_consume_stop"
    sl = _trigger(s, side, fill, -s.sl_usd)
    tp = _trigger(s, side, fill, s.tp_usd)
    gap = max(s.tick_size, min_stop_distance)
    if (
        sl <= 0
        or tp <= 0
        or side * (mark - sl) < gap - 1e-10
        or side * (tp - mark) < gap - 1e-10
    ):
        return None, "stop_too_close"
    invalid = d.invalidation + (spread if side < 0 else 0.0)
    clears = bool(d.zone_id) and side * (invalid - sl) >= s.tick_size - 1e-10
    if r.require_structural_stop and not clears:
        return None, "cash_stop_inside_structure"
    if d.objective is not None:
        exit_zone = d.objective + (spread if side < 0 else 0.0)
        if side * (exit_zone - tp) < -1e-10:
            return None, "opposing_zone_before_cash_target"
        if r.exit_mode == "next_zone":
            # Round into the zone's near edge, not beyond it.
            tp = round(
                (
                    floor(exit_zone / s.tick_size + 1e-9)
                    if side == 1
                    else ceil(exit_zone / s.tick_size - 1e-9)
                )
                * s.tick_size,
                12,
            )
    elif r.exit_mode == "next_zone":
        return None, "no_known_zone_target"
    structural_stop = structural_risk = indicative_lots = None
    if d.zone_id:
        unit = (invalid - side * s.tick_size) / s.tick_size
        structural_stop = round(
            (floor(unit + 1e-9) if side == 1 else ceil(unit - 1e-9)) * s.tick_size, 12
        )
        if structural_stop - side * s.slippage > 0:
            structural_risk = -pnl(s, side, fill, structural_stop - side * s.slippage)
            if structural_risk > 0:
                indicative_lots = s.lots * s.sl_usd / structural_risk
    return Plan(
        side,
        req,
        fill,
        sl,
        tp,
        floor_cost,
        clears,
        structural_stop,
        structural_risk,
        indicative_lots,
    ), "ready"


def reversal(d: Decision, side: int, r: Rules) -> bool:
    return (
        r.exit_mode == "next_zone"
        and side * d.momentum <= -r.momentum_min
        and any(direction == -side for _, direction in d.breaks)
    )


@dataclass
class EquityGuard:
    day: str = ""
    start: float = 0.0
    peak: float = 0.0
    day_halt: bool = False
    dd_halt: bool = False

    def mark(self, day: str, equity: float, limits: Limits):
        if not isfinite(equity) or equity <= 0:
            return "invalid_equity"
        if self.day != day:
            self.day, self.start, self.day_halt = day, equity, False
        self.peak = max(self.peak, equity)
        self.day_halt |= self.start - equity >= limits.daily_loss_usd - 1e-8
        self.dd_halt |= self.peak - equity >= limits.drawdown_usd - 1e-8
        return (
            "drawdown_halt"
            if self.dd_halt
            else "daily_loss_halt"
            if self.day_halt
            else ""
        )


def portfolio_block(s: Instrument, limits: Limits, legs: int, lots: float, risk: float):
    if legs + s.legs > limits.max_legs:
        return "portfolio_leg_limit"
    if lots + s.lots * s.legs > limits.max_total_lots + 1e-10:
        return "portfolio_lot_limit"
    if risk + s.sl_usd * s.legs > limits.max_risk_usd + 1e-8:
        return "portfolio_cash_risk_limit"
    return ""
