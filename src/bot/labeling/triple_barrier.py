from __future__ import annotations

import pandas as pd


def triple_barrier_labels(
    frame: pd.DataFrame,
    horizon_bars: int,
    take_profit_atr: float,
    stop_loss_atr: float,
) -> pd.Series:
    """Return 1 for TP first, 0 for SL first, and NA when neither barrier wins."""
    if horizon_bars < 1 or take_profit_atr <= 0 or stop_loss_atr <= 0:
        raise ValueError("Barrier distances and horizon must be positive")
    required = {"close", "high", "low", "atr_14"}
    if required.difference(frame.columns):
        raise ValueError(f"Label frame is missing columns: {sorted(required.difference(frame.columns))}")
    labels = pd.Series(pd.NA, index=frame.index, dtype="Int8")
    for position in range(len(frame) - horizon_bars):
        entry = frame["close"].iloc[position]
        atr = frame["atr_14"].iloc[position]
        if pd.isna(atr) or atr <= 0:
            continue
        upper = entry + float(atr) * take_profit_atr
        lower = entry - float(atr) * stop_loss_atr
        future = frame.iloc[position + 1 : position + horizon_bars + 1]
        for _, row in future.iterrows():
            hit_upper = row["high"] >= upper
            hit_lower = row["low"] <= lower
            if hit_upper and hit_lower:
                labels.iloc[position] = 0
                break
            if hit_upper:
                labels.iloc[position] = 1
                break
            if hit_lower:
                labels.iloc[position] = 0
                break
    return labels
