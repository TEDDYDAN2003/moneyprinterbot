from __future__ import annotations

import pandas as pd

from bot.data.bar_builder import build_timeframes, validate_ohlcv
from bot.time.sessions import session_flags


def _atr(frame: pd.DataFrame, period: int) -> pd.Series:
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(period, min_periods=period).mean()


def build_features(frame: pd.DataFrame, timeframe_minutes: int) -> pd.DataFrame:
    """Build features using only the current and preceding completed bars."""
    if timeframe_minutes < 1:
        raise ValueError("timeframe_minutes must be positive")
    source = validate_ohlcv(frame)
    result = pd.DataFrame(index=source.index)
    result["close"] = source["close"]
    result["return_1"] = source["close"].pct_change()
    result["range"] = source["high"] - source["low"]
    result["body"] = (source["close"] - source["open"]).abs()
    result["body_ratio"] = result["body"].div(result["range"].replace(0, pd.NA))
    result["atr_14"] = _atr(source, 14)
    result["ema_9"] = source["close"].ewm(span=9, adjust=False, min_periods=9).mean()
    result["ema_21"] = source["close"].ewm(span=21, adjust=False, min_periods=21).mean()
    result["ema_distance_atr"] = (result["ema_9"] - result["ema_21"]).div(
        result["atr_14"].replace(0, pd.NA)
    )
    result["direction"] = (source["close"] > source["open"]).astype("int8") - (
        source["close"] < source["open"]
    ).astype("int8")
    result["timeframe_minutes"] = timeframe_minutes
    return result


def build_multitimeframe_features(
    m1: pd.DataFrame, timeframes: tuple[int, ...] = (1, 3, 5, 10, 15)
) -> pd.DataFrame:
    """Align completed higher-timeframe features to each M1 close.

    A higher-timeframe feature is shifted by one completed bar before it is
    forward-filled, so a decision never sees the still-forming higher bar.
    """
    if 1 not in timeframes:
        raise ValueError("The base timeframe must include 1 minute")
    source = validate_ohlcv(m1)
    bars = build_timeframes(source, timeframes)
    base = build_features(bars[1], 1)
    result = base.copy()
    for minute in timeframes:
        if minute == 1:
            continue
        higher = build_features(bars[minute], minute).shift(1)
        higher = higher.add_prefix(f"tf{minute}_")
        result = result.join(higher.reindex(result.index, method="ffill"))
    result = result.join(session_flags(result.index))
    return result
