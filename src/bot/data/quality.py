from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class QualityReport:
    rows: int
    duplicate_timestamps: int
    invalid_ohlc_rows: int
    gaps_over_limit: int


def inspect_ohlcv(frame: pd.DataFrame, max_gap_minutes: int = 5) -> QualityReport:
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise TypeError("OHLCV index must be a DatetimeIndex")
    duplicate_count = int(frame.index.duplicated().sum())
    invalid = (
        (frame["high"] < frame[["open", "close"]].max(axis=1))
        | (frame["low"] > frame[["open", "close"]].min(axis=1))
        | (frame[["open", "high", "low", "close"]] <= 0).any(axis=1)
    )
    gaps = frame.index.to_series().diff().dt.total_seconds().div(60).gt(max_gap_minutes).sum()
    return QualityReport(len(frame), duplicate_count, int(invalid.sum()), int(gaps))
