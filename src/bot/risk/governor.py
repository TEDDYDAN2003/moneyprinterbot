from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskLimits:
    max_open_trades: int
    max_open_trades_per_direction: int
    max_daily_loss_pct: float
    max_equity_drawdown_kill_pct: float
    max_spread_points: float


@dataclass(frozen=True)
class AccountState:
    equity: float
    day_start_equity: float
    peak_equity: float
    open_trades: int
    open_long_trades: int
    open_short_trades: int
    spread_points: float


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str


class RiskGovernor:
    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits

    def can_open(self, state: AccountState, side: str) -> RiskDecision:
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"Unsupported side: {side}")
        if state.open_trades >= self._limits.max_open_trades:
            return RiskDecision(False, "max_open_trades")
        side_count = state.open_long_trades if side == "BUY" else state.open_short_trades
        if side_count >= self._limits.max_open_trades_per_direction:
            return RiskDecision(False, "max_open_trades_per_direction")
        if state.spread_points > self._limits.max_spread_points:
            return RiskDecision(False, "spread_limit")
        if state.day_start_equity <= 0 or state.equity <= 0:
            return RiskDecision(False, "invalid_equity")
        daily_loss_pct = (state.day_start_equity - state.equity) / state.day_start_equity * 100
        if daily_loss_pct >= self._limits.max_daily_loss_pct:
            return RiskDecision(False, "daily_loss_limit")
        drawdown_pct = (state.peak_equity - state.equity) / state.peak_equity * 100
        if drawdown_pct >= self._limits.max_equity_drawdown_kill_pct:
            return RiskDecision(False, "equity_drawdown_kill")
        return RiskDecision(True, "approved")
