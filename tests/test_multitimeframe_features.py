import pandas as pd

from bot.features.pipeline import build_multitimeframe_features


def bars(periods: int = 60) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=periods, freq="min", tz="UTC")
    close = pd.Series(range(100, 100 + periods), index=index, dtype=float)
    return pd.DataFrame(
        {"open": close - 0.1, "high": close + 0.2, "low": close - 0.2, "close": close},
        index=index,
    )


def test_higher_timeframe_features_are_not_available_while_bar_forms() -> None:
    features = build_multitimeframe_features(bars())
    assert pd.isna(features.loc[features.index[2], "tf3_ema_9"])
    assert pd.notna(features.loc[features.index[30], "tf3_ema_9"])


def test_future_price_changes_do_not_change_prior_multitimeframe_features() -> None:
    original = bars()
    changed = original.copy()
    changed.iloc[-1, changed.columns.get_loc("high")] = 10_000
    first = build_multitimeframe_features(original)
    second = build_multitimeframe_features(changed)
    pd.testing.assert_frame_equal(first.iloc[:-1], second.iloc[:-1])
