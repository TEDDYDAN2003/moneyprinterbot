from __future__ import annotations

from collections.abc import Iterable

import pandas as pd


OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


def validate_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"open", "high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"OHLCV data is missing columns: {sorted(missing)}")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise TypeError("OHLCV index must be a DatetimeIndex")
    if frame.index.tz is None:
        raise ValueError("OHLCV timestamps must be timezone-aware")
    if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        raise ValueError("OHLCV timestamps must be sorted and unique")
    return frame.sort_index()


def resample_ohlcv(frame: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if minutes < 1:
        raise ValueError("Timeframe must be a positive number of minutes")
    source = validate_ohlcv(frame)
    aggregation: dict[str, str] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    if "volume" in source:
        aggregation["volume"] = "sum"
    result = source.resample(f"{minutes}min", label="left", closed="left").agg(aggregation)
    return result.dropna(subset=["open", "high", "low", "close"])


def build_timeframes(
    m1: pd.DataFrame, minutes: Iterable[int] = (1, 3, 5, 10, 15)
) -> dict[int, pd.DataFrame]:
    source = validate_ohlcv(m1)
    return {minute: source.copy() if minute == 1 else resample_ohlcv(source, minute) for minute in minutes}
