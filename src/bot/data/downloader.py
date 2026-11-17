from __future__ import annotations

from pathlib import Path

import pandas as pd

from bot.data.bar_builder import validate_ohlcv


def load_ohlcv_csv(path: str | Path, timestamp_column: str = "timestamp") -> pd.DataFrame:
    source = pd.read_csv(path)
    if timestamp_column not in source:
        raise ValueError(f"CSV is missing timestamp column: {timestamp_column}")
    timestamps = pd.to_datetime(source.pop(timestamp_column), utc=True, errors="raise")
    source.index = pd.DatetimeIndex(timestamps, name="timestamp")
    source.columns = [str(column).lower() for column in source.columns]
    return validate_ohlcv(source)


def save_ohlcv_parquet(frame: pd.DataFrame, path: str | Path) -> None:
    validated = validate_ohlcv(frame)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    validated.to_parquet(output)
