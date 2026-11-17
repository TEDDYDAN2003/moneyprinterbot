from __future__ import annotations

import math
from collections.abc import Iterable


def summarize_pnl(pnl: Iterable[float]) -> dict[str, float]:
    values = [float(value) for value in pnl]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "trades": float(len(values)),
        "win_rate": len(wins) / len(values) if values else 0.0,
        "net_profit": sum(values),
        "profit_factor": gross_profit / gross_loss if gross_loss else math.inf,
        "average_win": gross_profit / len(wins) if wins else 0.0,
        "average_loss": -gross_loss / len(losses) if losses else 0.0,
        "max_drawdown": max_drawdown,
    }
