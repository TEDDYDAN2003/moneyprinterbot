from datetime import datetime, timezone

from bot.backtest.metrics import summarize_pnl
from bot.execution.broker_base import OrderRequest
from bot.execution.sim_broker import SimBroker


def test_sim_broker_records_net_pnl() -> None:
    broker = SimBroker(commission_per_unit=0.1)
    opened = broker.open(
        OrderRequest("BUY", 2, 100, 99, 102),
        datetime.now(timezone.utc),
    )
    pnl = broker.close(opened.id, 101, datetime.now(timezone.utc))
    assert pnl == 1.8
    assert summarize_pnl([pnl])["net_profit"] == 1.8
