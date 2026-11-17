import pandas as pd
import pytest

from bot.data.bar_builder import build_timeframes
from bot.data.quality import inspect_ohlcv
from bot.features.pipeline import build_features
from bot.labeling.triple_barrier import triple_barrier_labels


def sample_bars() -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=30, freq="min", tz="UTC")
    close = pd.Series(range(100, 130), index=index, dtype=float)
    return pd.DataFrame(
        {
            "open": close - 0.25,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1.0,
        },
        index=index,
    )


def test_timeframe_builder_and_quality_report() -> None:
    bars = sample_bars()
    built = build_timeframes(bars)
    assert len(built[3]) == 10
    report = inspect_ohlcv(bars)
    assert report.rows == 30
    assert report.invalid_ohlc_rows == 0


def test_features_do_not_change_when_future_bars_change() -> None:
    bars = sample_bars()
    original = build_features(bars, 1)
    changed = bars.copy()
    changed.iloc[-1, changed.columns.get_loc("high")] = 10_000
    changed_features = build_features(changed, 1)
    pd.testing.assert_frame_equal(original.iloc[:-1], changed_features.iloc[:-1])


def test_triple_barrier_uses_conservative_same_bar_order() -> None:
    bars = sample_bars().iloc[:16].copy()
    features = build_features(bars, 1)
    features.loc[features.index[13], "atr_14"] = 1.0
    features.loc[features.index[14], ["high", "low"]] = [102.0, 98.0]
    labels = triple_barrier_labels(features, 2, 1.0, 1.0)
    assert labels.iloc[13] == 0


def test_naive_index_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_features(sample_bars().tz_localize(None), 1)
