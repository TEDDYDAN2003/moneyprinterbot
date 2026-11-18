from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1] / "results" / "target_structure_prediction_v1"
FEATURES = ROOT / "feature_cache"
OUT = ROOT / "level_pullback_policy_backtest.csv"
TP_PRICE_UNITS = 5.0
LOT = 0.05
CONTRACT_OZ = 100.0
ROUND_TRIP_COST_PRICE_EQUIVALENT = 0.77
MIN_CALIBRATED_PROBABILITY = 0.30


def metrics(frame: pd.DataFrame, name: str, tf: str, period: str) -> dict:
    trades = frame.sort_values("entry_idx")
    kept: list[pd.Series] = []
    next_entry = -1
    for row in trades.itertuples(index=False):
        if int(row.entry_idx) <= next_entry:
            continue
        kept.append(pd.Series(row._asdict()))
        next_entry = int(row.exit_idx)
    if not kept:
        return {"timeframe": tf, "period": period, "policy": name, "trades": 0,
                "win_rate_net": 0.0, "net_profit_usd": 0.0, "expectancy_usd": 0.0,
                "profit_factor": 0.0, "max_drawdown_pct": 0.0, "costs_usd": 0.0}
    selected = pd.DataFrame(kept)
    gross_price_move = np.where(
        selected["target_hit"].astype(bool),
        TP_PRICE_UNITS,
        selected["side"].astype(float) * (selected["label_exit_price"].astype(float) - selected["entry_price"].astype(float)),
    )
    costs = ROUND_TRIP_COST_PRICE_EQUIVALENT * LOT * CONTRACT_OZ
    net = gross_price_move * LOT * CONTRACT_OZ - costs
    equity = 10_000.0 + np.cumsum(net)
    peaks = np.maximum.accumulate(np.r_[10_000.0, equity])[1:]
    drawdown = (peaks - equity) / peaks
    winners = net[net > 0]
    losers = net[net < 0]
    return {
        "timeframe": tf,
        "period": period,
        "policy": name,
        "trades": len(net),
        "win_rate_net": float((net > 0).mean()),
        "net_profit_usd": float(net.sum()),
        "expectancy_usd": float(net.mean()),
        "profit_factor": float(winners.sum() / abs(losers.sum())) if len(losers) else float("inf"),
        "max_drawdown_pct": float(drawdown.max() * 100.0) if len(drawdown) else 0.0,
        "costs_usd": float(costs * len(net)),
    }


def main() -> None:
    rows = []
    for tf, minutes in (("M1", 1), ("M5", 5), ("M15", 15)):
        events = pd.read_parquet(ROOT / f"oos_{tf}_tp5.parquet")
        feat = pd.read_parquet(FEATURES / f"features_{tf}.parquet")
        at = pd.to_datetime(events["entry_time"], utc=True)
        aligned = feat.reindex(at)
        prefix = f"m{minutes}_"
        side = events["side"].astype(float).to_numpy()
        ema = aligned[f"{prefix}ema9_21"].to_numpy(float)
        pressure = aligned[f"{prefix}body_pressure_3"].to_numpy(float)
        lower_wick = aligned[f"{prefix}lower_wick"].to_numpy(float)
        upper_wick = aligned[f"{prefix}upper_wick"].to_numpy(float)
        swing_low = aligned["confirmed_swing_low_dist_atr"].to_numpy(float)
        swing_high = aligned["confirmed_swing_high_dist_atr"].to_numpy(float)
        roll_low = aligned["structure_12_low_dist_atr"].to_numpy(float)
        roll_high = aligned["structure_12_high_dist_atr"].to_numpy(float)
        probability = events["p_calibrated"].to_numpy(float)
        # Signed close-to-level distances are converted to absolute proximity.
        # Direction, level location, and completed-candle response are fixed here;
        # no threshold sweep is run on the outcome period.
        support_near = np.minimum(np.abs(swing_low), np.abs(roll_low)) <= 0.50
        resistance_near = np.minimum(np.abs(swing_high), np.abs(roll_high)) <= 0.50
        with_trend = side * ema > 0
        rejection = np.where(
            side > 0,
            (pressure >= 0.08) | ((lower_wick > upper_wick) & (pressure > -0.05)),
            (pressure <= -0.08) | ((upper_wick > lower_wick) & (pressure < 0.05)),
        )
        level_pullback = np.where(side > 0, support_near, resistance_near)
        candidates = events.loc[probability >= MIN_CALIBRATED_PROBABILITY].copy()
        candidates["entry_idx"] = candidates["entry_idx"].astype(int)
        candidates["exit_idx"] = candidates["exit_idx"].astype(int)
        accept = (probability >= MIN_CALIBRATED_PROBABILITY) & with_trend & level_pullback & rejection
        level_candidates = events.loc[accept].copy()
        for label, subset in (("walkforward_probability_baseline", candidates),
                              ("trend_pullback_level_filter", level_candidates)):
            for period, start, end in (
                ("all_oos", "1900-01-01", "2100-01-01"),
                ("pre_inspected", "2024-10-01", "2026-05-21"),
                ("previously_inspected", "2026-05-21", "2100-01-01"),
            ):
                mask = pd.to_datetime(subset["entry_time"], utc=True).between(
                    pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"), inclusive="left"
                )
                rows.append(metrics(subset.loc[mask], label, tf, period))
    summary = pd.DataFrame(rows)
    summary.to_csv(OUT, index=False)
    print(summary.to_string(index=False))
    print(json.dumps({"output": str(OUT), "friction_price_equivalent": ROUND_TRIP_COST_PRICE_EQUIVALENT,
                      "lot": LOT, "tp_price_units": TP_PRICE_UNITS,
                      "min_calibrated_probability": MIN_CALIBRATED_PROBABILITY,
                      "entry_filter": "trend-aligned EMA9/21 + <=0.50 ATR confirmed/rolling local level + completed-bar rejection",
                      "note": "walk-forward OOS rows; exploratory research period has been previously inspected"}, indent=2))


if __name__ == "__main__":
    main()
