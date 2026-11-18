from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1] / "results" / "target_structure_prediction_v1"
FEATURES = ROOT / "feature_cache"
OUT = ROOT / "cash_pullback_entries_backtest.csv"
LOT = 0.05
CONTRACT_OZ = 100.0
STOP_USD = 2.50
TARGET_USD = 5.00
EX_SPREAD_COST_PRICE = 0.27  # $0.20 slippage + $0.07/oz-lot round-turn commission equivalent
MIN_CALIBRATED_PROBABILITY = 0.30
INITIAL_EQUITY = 1127.64  # Current connected demo-account balance; fixed-size, non-compounded replay.


def replay(frame: pd.DataFrame, opens: np.ndarray, highs: np.ndarray, lows: np.ndarray,
           closes: np.ndarray, spread_price: np.ndarray, horizon: int) -> dict:
    frame = frame.sort_values("entry_idx")
    stop_distance = STOP_USD / (LOT * CONTRACT_OZ)
    target_distance = TARGET_USD / (LOT * CONTRACT_OZ)
    next_free = -1
    pnls: list[float] = []
    costs: list[float] = []
    exits: list[int] = []
    for row in frame.itertuples(index=False):
        idx = int(row.entry_idx)
        if idx <= next_free or idx < 0 or idx >= len(opens):
            continue
        side = int(row.side)
        entry = float(row.entry_price)
        sl = entry - side * stop_distance
        tp = entry + side * target_distance
        end = min(idx + max(1, int(horizon)), len(opens) - 1)
        exit_price = float(closes[end])
        exit_idx = end
        for bar in range(idx, end + 1):
            opened, high, low = float(opens[bar]), float(highs[bar]), float(lows[bar])
            if side > 0:
                stop_hit, target_hit = low <= sl, high >= tp
                if opened <= sl:
                    exit_price, exit_idx = opened, bar
                    break
                if stop_hit:  # if both barriers are touched in one OHLC bar, count the stop first
                    exit_price, exit_idx = sl, bar
                    break
                if target_hit:
                    exit_price, exit_idx = tp, bar
                    break
            else:
                stop_hit, target_hit = high >= sl, low <= tp
                if opened >= sl:
                    exit_price, exit_idx = opened, bar
                    break
                if stop_hit:
                    exit_price, exit_idx = sl, bar
                    break
                if target_hit:
                    exit_price, exit_idx = tp, bar
                    break
        spread = max(0.0, float(spread_price[idx]))
        cash_cost = (spread + EX_SPREAD_COST_PRICE) * LOT * CONTRACT_OZ
        pnls.append(side * (exit_price - entry) * LOT * CONTRACT_OZ - cash_cost)
        costs.append(cash_cost)
        exits.append(exit_idx)
        next_free = exit_idx
    if not pnls:
        return {"trades": 0, "win_rate_net": 0.0, "net_profit_usd": 0.0, "expectancy_usd": 0.0,
                "profit_factor": 0.0, "max_drawdown_pct": 0.0, "average_cost_usd": 0.0}
    net = np.asarray(pnls, dtype=float)
    equity = INITIAL_EQUITY + np.cumsum(net)
    peaks = np.maximum.accumulate(np.r_[INITIAL_EQUITY, equity])[1:]
    dd = (peaks - equity) / peaks
    gains, losses = net[net > 0], net[net < 0]
    return {
        "trades": len(net),
        "win_rate_net": float((net > 0).mean()),
        "net_profit_usd": float(net.sum()),
        "expectancy_usd": float(net.mean()),
        "profit_factor": float(gains.sum() / abs(losses.sum())) if len(losses) else float("inf"),
        "max_drawdown_pct": float(dd.max() * 100.0),
        "average_cost_usd": float(np.mean(costs)),
    }


def main() -> None:
    results = []
    for tf, minutes in (("M1", 1), ("M5", 5), ("M15", 15)):
        events = pd.read_parquet(ROOT / f"oos_{tf}_tp5.parquet")
        features = pd.read_parquet(FEATURES / f"features_{tf}.parquet")
        raw = pd.read_parquet(FEATURES / "base_M1.parquet")
        times = pd.to_datetime(events["entry_time"], utc=True)
        at_entry = features.reindex(times)
        side = events["side"].to_numpy(int)
        prefix = f"m{minutes}_"
        ema = at_entry[f"{prefix}ema9_21"].to_numpy(float)
        pressure = at_entry[f"{prefix}body_pressure_3"].to_numpy(float)
        lower = at_entry[f"{prefix}lower_wick"].to_numpy(float)
        upper = at_entry[f"{prefix}upper_wick"].to_numpy(float)
        support_distance = np.minimum(
            np.abs(at_entry["confirmed_swing_low_dist_atr"].to_numpy(float)),
            np.abs(at_entry["structure_12_low_dist_atr"].to_numpy(float)),
        )
        resistance_distance = np.minimum(
            np.abs(at_entry["confirmed_swing_high_dist_atr"].to_numpy(float)),
            np.abs(at_entry["structure_12_high_dist_atr"].to_numpy(float)),
        )
        near_level = np.where(side > 0, support_distance <= 0.50, resistance_distance <= 0.50)
        response = np.where(
            side > 0,
            (pressure >= 0.08) | ((lower > upper) & (pressure > -0.05)),
            (pressure <= -0.08) | ((upper > lower) & (pressure < 0.05)),
        )
        trend_ok = side * ema > 0
        probability_ok = events["p_calibrated"].to_numpy(float) >= MIN_CALIBRATED_PROBABILITY
        pullback_mask = probability_ok & trend_ok & near_level & response
        baseline = events.loc[probability_ok].copy()
        pullback = events.loc[pullback_mask].copy()
        opens = raw["open"].to_numpy(float)
        highs = raw["high"].to_numpy(float)
        lows = raw["low"].to_numpy(float)
        closes = raw["close"].to_numpy(float)
        # The source rates store spread in points; XAUUSDm uses a 0.001 point.
        entry_spreads = np.maximum(0.0, np.nan_to_num(raw["spread"].to_numpy(float) * 0.001, nan=0.5))
        for period, start, end in (
            ("all_oos", "2024-10-01", "2100-01-01"),
            ("pre_inspected", "2024-10-01", "2026-05-21"),
            ("previously_inspected", "2026-05-21", "2100-01-01"),
        ):
            for policy, subset in (("probability_baseline", baseline), ("trend_pullback_filter", pullback)):
                selected = pd.to_datetime(subset["entry_time"], utc=True).between(
                    pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"), inclusive="left"
                )
                result = replay(
                    subset.loc[selected], opens, highs, lows, closes,
                    entry_spreads, int({"M1": 15, "M5": 30, "M15": 60}[tf]),
                )
                results.append({"timeframe": tf, "period": period, "policy": policy,
                                "lot": LOT, "cash_stop_usd": STOP_USD,
                                "cash_target_usd": TARGET_USD,
                                "max_hold_m1_bars": int({"M1": 15, "M5": 30, "M15": 60}[tf]),
                                **result})
    summary = pd.DataFrame(results)
    summary.to_csv(OUT, index=False)
    print(summary.to_string(index=False))
    print(f"Saved {OUT}")


if __name__ == "__main__":
    main()
