from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from bot.execution.broker_base import OrderRequest
from bot.execution.sim_broker import SimBroker
from bot.risk.governor import AccountState, RiskGovernor


Signal = Callable[[pd.Timestamp, pd.Series], OrderRequest | None]


@dataclass(frozen=True)
class BacktestResult:
    trades: tuple[float, ...]
    rejected_signals: tuple[str, ...]


def run_backtest(
    bars: pd.DataFrame,
    features: pd.DataFrame,
    signal: Signal,
    governor: RiskGovernor,
    initial_equity: float = 10_000.0,
    commission_per_unit: float = 0.0,
) -> BacktestResult:
    if not bars.index.equals(features.index):
        raise ValueError("Bars and features must have identical indexes")
    broker = SimBroker(commission_per_unit)
    equity = initial_equity
    peak_equity = initial_equity
    rejected: list[str] = []
    for timestamp, bar in bars.iterrows():
        _close_triggered_positions(broker, bar, timestamp)
        if broker.closed:
            equity = initial_equity + sum(trade.pnl for trade in broker.closed)
            peak_equity = max(peak_equity, equity)
        request = signal(timestamp, features.loc[timestamp])
        if request is None:
            continue
        long_count = sum(position.request.side == "BUY" for position in broker.positions)
        short_count = sum(position.request.side == "SELL" for position in broker.positions)
        state = AccountState(
            equity=equity,
            day_start_equity=initial_equity,
            peak_equity=peak_equity,
            open_trades=len(broker.positions),
            open_long_trades=long_count,
            open_short_trades=short_count,
            spread_points=0.0,
        )
        decision = governor.can_open(state, request.side)
        if decision.allowed:
            broker.open(request, timestamp)
        else:
            rejected.append(decision.reason)
    if len(bars):
        timestamp = bars.index[-1]
        last_close = float(bars["close"].iloc[-1])
        for position in tuple(broker.positions):
            broker.close(position.id, last_close, timestamp)
    return BacktestResult(tuple(trade.pnl for trade in broker.closed), tuple(rejected))


def _close_triggered_positions(broker: SimBroker, bar: pd.Series, timestamp: Any) -> None:
    for position in tuple(broker.positions):
        request = position.request
        if request.side == "BUY":
            stop_hit = float(bar["low"]) <= request.stop_loss
            target_hit = float(bar["high"]) >= request.take_profit
        else:
            stop_hit = float(bar["high"]) >= request.stop_loss
            target_hit = float(bar["low"]) <= request.take_profit
        if stop_hit:
            broker.close(position.id, request.stop_loss, timestamp)
        elif target_hit:
            broker.close(position.id, request.take_profit, timestamp)
