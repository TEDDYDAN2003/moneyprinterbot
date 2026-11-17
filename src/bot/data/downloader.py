from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.data.bar_builder import validate_ohlcv
from bot.data.mt5_client import MT5Client


def load_ohlcv_csv(path: str | Path, timestamp_column: str = "timestamp") -> pd.DataFrame:
    source = pd.read_csv(path)
    if timestamp_column not in source:
        raise ValueError(f"CSV is missing timestamp column: {timestamp_column}")
    timestamps = pd.to_datetime(source.pop(timestamp_column), utc=True, errors="raise")
    source.index = pd.DatetimeIndex(timestamps, name="timestamp")
    source.columns = [str(column).lower() for column in source.columns]
    return validate_ohlcv(source)


def load_ohlcv(path: str | Path, timestamp_column: str = "timestamp") -> pd.DataFrame:
    input_path = Path(path)
    if input_path.suffix.lower() in {".parquet", ".pq"}:
        return validate_ohlcv(pd.read_parquet(input_path))
    return load_ohlcv_csv(input_path, timestamp_column)


def save_ohlcv_parquet(frame: pd.DataFrame, path: str | Path) -> None:
    validated = validate_ohlcv(frame)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    validated.to_parquet(output)


def fetch_mt5_m1(
    symbol: str,
    years: int,
    terminal_path: str | None = None,
    now: datetime | None = None,
    minimum_history_days_per_year: int = 300,
    allow_partial: bool = False,
) -> pd.DataFrame:
    if years < 1:
        raise ValueError("years must be positive")
    client = MT5Client(terminal_path)
    client.connect()
    try:
        end = now or datetime.now(timezone.utc)
        start = end - timedelta(days=365 * years)
        import MetaTrader5 as mt5

        chunks: list[pd.DataFrame] = []
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=31), end)
            raw = client.bars(symbol, mt5.TIMEFRAME_M1, cursor, chunk_end)
            if len(raw):
                chunk = pd.DataFrame(raw)
                chunk["timestamp"] = pd.to_datetime(chunk["time"], unit="s", utc=True)
                chunk = chunk.set_index("timestamp")
                chunk = chunk.rename(columns={"tick_volume": "volume"})
                chunks.append(chunk[["open", "high", "low", "close", "volume"]])
            cursor = chunk_end
        if not chunks:
            raise RuntimeError(f"MT5 returned no M1 bars for {symbol}")
        combined = pd.concat(chunks)
        combined = combined.loc[~combined.index.duplicated()].sort_index()
        validated = validate_ohlcv(combined)
        available_days = (validated.index[-1] - validated.index[0]).total_seconds() / 86400
        required_days = years * minimum_history_days_per_year
        if available_days < required_days and not allow_partial:
            raise RuntimeError(
                f"MT5 returned {available_days:.0f} days for a {years}-year request. "
                "Increase MT5 Max bars in chart and ensure older XAUUSDm history is downloaded."
            )
        return validated
    finally:
        client.close()
