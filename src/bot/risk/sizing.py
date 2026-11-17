from __future__ import annotations

from dataclasses import dataclass
from math import floor


@dataclass(frozen=True)
class SizingRules:
    min_lot: float
    max_lot_per_trade: float
    lot_step: float
    max_total_lots_open: float


def size_from_risk(
    equity: float,
    risk_pct: float,
    stop_distance: float,
    value_per_price_unit_per_lot: float,
    open_lots: float,
    rules: SizingRules,
) -> float:
    if min(equity, risk_pct, stop_distance, value_per_price_unit_per_lot) <= 0:
        raise ValueError("Equity, risk, stop distance, and contract value must be positive")
    if open_lots < 0:
        raise ValueError("open_lots cannot be negative")
    risk_budget = equity * risk_pct / 100
    raw_lots = risk_budget / (stop_distance * value_per_price_unit_per_lot)
    remaining = rules.max_total_lots_open - open_lots
    capped = min(raw_lots, rules.max_lot_per_trade, remaining)
    if capped < rules.min_lot:
        return 0.0
    stepped = floor(capped / rules.lot_step + 1e-9) * rules.lot_step
    return round(max(stepped, rules.min_lot), 10)
