from bot.risk.governor import AccountState, RiskGovernor, RiskLimits


def governor() -> RiskGovernor:
    return RiskGovernor(
        RiskLimits(
            max_open_trades=3,
            max_open_trades_per_direction=2,
            max_daily_loss_pct=2.0,
            max_equity_drawdown_kill_pct=8.0,
            max_spread_points=35,
        )
    )


def state(**updates: float) -> AccountState:
    values = {
        "equity": 10_000,
        "day_start_equity": 10_000,
        "peak_equity": 10_000,
        "open_trades": 0,
        "open_long_trades": 0,
        "open_short_trades": 0,
        "spread_points": 10,
    }
    values.update(updates)
    return AccountState(**values)


def test_governor_allows_safe_order() -> None:
    assert governor().can_open(state(), "BUY").allowed


def test_governor_blocks_daily_loss_and_spread() -> None:
    assert governor().can_open(state(equity=9_750), "BUY").reason == "daily_loss_limit"
    assert governor().can_open(state(spread_points=36), "BUY").reason == "spread_limit"


def test_governor_blocks_directional_exposure() -> None:
    assert governor().can_open(state(open_long_trades=2), "BUY").reason == "max_open_trades_per_direction"
