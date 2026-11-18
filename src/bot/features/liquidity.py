from __future__ import annotations

import pandas as pd


def build_liquidity_features(
    frame: pd.DataFrame,
    lookback: int = 30,
    tolerance_atr: float = 0.15,
    stop_zone_offset_atr: tuple[float, float] = (0.1, 0.3),
    sweep_confirm_bars: int = 3,
    min_sweep_depth_atr: float = 0.05,
) -> pd.DataFrame:
    """Build liquidity observations available at the current bar close only."""
    if lookback < 2 or sweep_confirm_bars < 1:
        raise ValueError("lookback and sweep_confirm_bars must be positive")
    if not 0 < tolerance_atr or not 0 < min_sweep_depth_atr:
        raise ValueError("liquidity tolerances must be positive")
    if len(stop_zone_offset_atr) != 2 or not 0 < stop_zone_offset_atr[0] <= stop_zone_offset_atr[1]:
        raise ValueError("stop_zone_offset_atr must be an increasing pair")
    required = {"open", "high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Liquidity frame is missing columns: {sorted(missing)}")
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.rolling(14, min_periods=14).mean()
    prior_high = frame["high"].shift(1).rolling(lookback, min_periods=2).max()
    prior_low = frame["low"].shift(1).rolling(lookback, min_periods=2).min()
    prior_high_count = (
        frame["high"].shift(1).sub(prior_high).abs().le(atr * tolerance_atr).rolling(lookback).sum()
    )
    prior_low_count = (
        frame["low"].shift(1).sub(prior_low).abs().le(atr * tolerance_atr).rolling(lookback).sum()
    )
    upper_sweep = (
        frame["high"].gt(prior_high + atr * min_sweep_depth_atr)
        & frame["close"].le(prior_high)
    )
    lower_sweep = (
        frame["low"].lt(prior_low - atr * min_sweep_depth_atr)
        & frame["close"].ge(prior_low)
    )
    sweep = upper_sweep | lower_sweep
    result = pd.DataFrame(index=frame.index)
    result["buy_side_pool"] = prior_high
    result["sell_side_pool"] = prior_low
    result["buy_side_pool_size"] = prior_high_count
    result["sell_side_pool_size"] = prior_low_count
    result["buy_side_stop_zone_low"] = prior_high + atr * stop_zone_offset_atr[0]
    result["buy_side_stop_zone_high"] = prior_high + atr * stop_zone_offset_atr[1]
    result["sell_side_stop_zone_low"] = prior_low - atr * stop_zone_offset_atr[1]
    result["sell_side_stop_zone_high"] = prior_low - atr * stop_zone_offset_atr[0]
    result["sweep_occurred"] = sweep.astype("int8")
    result["upper_sweep"] = upper_sweep.astype("int8")
    result["lower_sweep"] = lower_sweep.astype("int8")
    result["sweep_depth_atr"] = pd.concat(
        [
            (frame["high"] - prior_high).div(atr),
            (prior_low - frame["low"]).div(atr),
        ],
        axis=1,
    ).max(axis=1).where(sweep, 0.0)
    result["close_back_inside"] = (upper_sweep | lower_sweep).astype("int8")
    result["time_since_sweep"] = _bars_since(sweep, sweep_confirm_bars)
    result["rejection_wick_ratio"] = (
        frame["high"].sub(frame[["open", "close"]].max(axis=1))
        .add(frame[["open", "close"]].min(axis=1).sub(frame["low"]))
        .div(frame["high"].sub(frame["low"]).replace(0, pd.NA))
    )
    result["displacement_score"] = frame["close"].sub(frame["open"]).abs().div(atr)
    result["draw_to_buy_side_atr"] = prior_high.sub(frame["close"]).div(atr)
    result["draw_to_sell_side_atr"] = frame["close"].sub(prior_low).div(atr)
    result["fvg_bullish"] = (frame["low"] > frame["high"].shift(2)).astype("int8")
    result["fvg_bearish"] = (frame["high"] < frame["low"].shift(2)).astype("int8")
    return result


def _bars_since(events: pd.Series, cap: int) -> pd.Series:
    values: list[int] = []
    elapsed = cap + 1
    for event in events.fillna(False):
        elapsed = 0 if event else elapsed + 1
        values.append(min(elapsed, cap + 1))
    return pd.Series(values, index=events.index, dtype="int64")
