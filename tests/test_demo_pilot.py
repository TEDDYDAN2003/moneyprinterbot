from types import SimpleNamespace

import pytest
import numpy as np
import pandas as pd

from bot.decision.market_aware import evaluate_market
from bot.features.structure import structure
from bot.execution.demo_runner import (
    DemoSafetyError,
    _account_type,
    _build_order,
    _validate_account_mode,
    _validate_run_mode,
    _ensure_same_demo_account,
)


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    SYMBOL_FILLING_FOK = 1

    @staticmethod
    def order_calc_profit(order_type, symbol, volume, entry, exit_price):
        direction = 1 if order_type == FakeMT5.ORDER_TYPE_BUY else -1
        return (exit_price - entry) * direction * 100 * volume


def test_account_type_is_detected_from_connected_terminal() -> None:
    for trade_mode, expected in ((0, "demo"), (1, "contest"), (2, "real")):
        assert _account_type(FakeMT5, SimpleNamespace(trade_mode=trade_mode)) == expected


def test_account_type_rejects_missing_or_unrecognized_account() -> None:
    with pytest.raises(DemoSafetyError, match="did not return account"):
        _account_type(FakeMT5, None)
    with pytest.raises(DemoSafetyError, match="unrecognized"):
        _account_type(FakeMT5, SimpleNamespace(trade_mode=-1))


def test_armed_runner_stops_if_mt5_account_changes() -> None:
    account = SimpleNamespace(trade_mode=0, login=123, server="Demo-Server")
    _ensure_same_demo_account(FakeMT5, account, 123, "Demo-Server")
    with pytest.raises(DemoSafetyError, match="account changed"):
        _ensure_same_demo_account(FakeMT5, account, 456, "Demo-Server")
    with pytest.raises(DemoSafetyError, match="non-demo"):
        _ensure_same_demo_account(FakeMT5, SimpleNamespace(trade_mode=2, login=123, server="Demo-Server"), 123, "Demo-Server")


def test_real_and_contest_accounts_cannot_be_armed() -> None:
    _validate_account_mode("demo", armed=True)
    for account_type in ("real", "contest", "unknown"):
        with pytest.raises(DemoSafetyError):
            _validate_account_mode(account_type, armed=True)


@pytest.mark.parametrize("mode", ["live", "auto", "demo", "paper"])
def test_legacy_runner_is_observation_only(mode) -> None:
    _validate_run_mode(mode, armed=False)
    with pytest.raises(DemoSafetyError, match="shadow-only"):
        _validate_run_mode(mode, armed=True)


def test_order_volume_never_exceeds_risk_and_prices_round_outward() -> None:
    symbol_info = SimpleNamespace(
        digits=2,
        point=0.01,
        volume_step=0.01,
        volume_min=0.01,
        volume_max=100.0,
        filling_mode=1,
    )
    tick = SimpleNamespace(ask=2000.01, bid=1999.99)
    account = SimpleNamespace(equity=10_000.0)
    decision = SimpleNamespace(side="BUY", stop_loss=1995.037, take_profit=2006.123)
    request, risk = _build_order(
        FakeMT5,
        "XAUUSD",
        symbol_info,
        tick,
        account,
        decision,
        {"risk_per_trade_pct": 0.10, "max_lot_per_trade": 0.01, "commission_round_turn_per_lot": 7.0},
        {"max_lot_per_trade": 0.50},
        {"deviation_points": 20},
    )
    assert request is not None
    assert request["sl"] == 1995.03
    assert request["tp"] == 2006.13
    assert request["volume"] == 0.01
    assert risk <= 10.0


def test_order_is_skipped_when_broker_minimum_exceeds_risk_budget() -> None:
    symbol_info = SimpleNamespace(
        digits=2,
        point=0.01,
        volume_step=0.01,
        volume_min=0.01,
        volume_max=100.0,
        filling_mode=1,
    )
    tick = SimpleNamespace(ask=2000.01, bid=1999.99)
    account = SimpleNamespace(equity=500.0)
    decision = SimpleNamespace(side="BUY", stop_loss=1995.0, take_profit=2007.5)
    request, _ = _build_order(
        FakeMT5,
        "XAUUSD",
        symbol_info,
        tick,
        account,
        decision,
        {"risk_per_trade_pct": 0.10, "max_lot_per_trade": 0.01},
        {"max_lot_per_trade": 0.50},
        {"deviation_points": 20},
    )
    assert request is None


def test_market_scorer_handles_all_researched_timeframes_causally() -> None:
    size = 1800
    index = pd.date_range("2026-11-18", periods=size, freq="min", tz="UTC")
    x = np.arange(size, dtype=float)
    close = 2650 + 0.003 * x + 1.2 * np.sin(x / 18)
    open_price = close - 0.08 * np.sin(x / 4)
    bars = pd.DataFrame(
        {
            "open": open_price,
            "high": np.maximum(open_price, close) + 0.18,
            "low": np.minimum(open_price, close) - 0.18,
            "close": close,
            "volume": 100 + 30 * (1 + np.sin(x / 9)),
            "spread": 20,
        },
        index=index,
    )
    for timeframe in ("M1", "M5", "M15"):
        decision = evaluate_market(bars, timeframe, float(close[-1] - 0.1), float(close[-1] + 0.1), sessions={})
        assert decision.bar_time.tzinfo is not None
        assert 0 <= decision.score <= 1
        if decision.side is not None:
            assert decision.stop_loss is not None
            assert decision.take_profit is not None

    changed = bars.copy()
    changed.iloc[1200:, changed.columns.get_loc("high")] += 1000
    original_state, _ = structure(bars, "M1")
    changed_state, _ = structure(changed, "M1")
    pd.testing.assert_frame_equal(original_state.iloc[:1200], changed_state.iloc[:1200])


