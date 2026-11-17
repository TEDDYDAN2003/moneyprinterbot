import pandas as pd

from bot.backtest.engine import run_backtest
from bot.execution.broker_base import OrderRequest
from bot.risk.governor import RiskGovernor, RiskLimits


def test_backtest_closes_stop_before_target_on_same_bar() -> None:
    index = pd.date_range("2026-01-01", periods=2, freq="min", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": [100.0, 100.0],
            "high": [100.0, 102.0],
            "low": [100.0, 98.0],
            "close": [100.0, 100.0],
        },
        index=index,
    )
    governor = RiskGovernor(RiskLimits(1, 1, 100, 100, 100))
    result = run_backtest(
        bars,
        bars,
        lambda timestamp, _: OrderRequest("BUY", 1, 100, 99, 101) if timestamp == index[0] else None,
        governor,
    )
    assert result.trades == (-1.0,)
