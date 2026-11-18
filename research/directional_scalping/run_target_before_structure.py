"""Research P(target before a causally available protected stop).

Research-only. This runner reuses the existing confluence feature builder and
causal directional-change structure engine. It does not alter production code,
models, or MT5 state. See the JSON manifest written beside the outputs for the
frozen labels, walk-forward/calibration windows, execution assumptions, and
known inferential limits.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
INPUT = HERE / "inputs"
RESULTS = HERE / "results"
OUT = RESULTS / "target_structure_prediction_v1"
DATA = ROOT / "data/bars/xauusdm_m1_3y_20261117.parquet"
EVENT_FILE = ROOT / "data/events/bls_cpi_ppi_actual_2023_2026.csv"
FEATURE_SCRIPT = INPUT / "research_m1_m15_events.py"
ENGINE = HERE / "structure_engine.py"
OLD_MANIFEST = RESULTS / "stop_comparison_manifest.json"
TF_HORIZON = {"M1": 15, "M5": 30, "M15": 60}
TARGETS = (1.50, 2.50, 5.00, 10.00)  # XAUUSD price units, as in prior studies
FIXED_SL = 0.75
SPREAD_PRICE = 0.50
SLIPPAGE_PER_SIDE = 0.10
COMMISSION_PER_LOT = 7.0
CONTRACT_OZ = 100.0
COST_PRICE = SPREAD_PRICE + 2 * SLIPPAGE_PER_SIDE + COMMISSION_PER_LOT / CONTRACT_OZ
INITIAL_EQUITY = 10_000.0
RISK_FRACTION = 0.0025
DAILY_LOSS_LIMIT = 0.02
WEEKLY_LOSS_LIMIT = 0.05
PEAK_DRAWDOWN_LIMIT = 0.08
LOT_STEP = 0.01
MIN_LOT = 0.01
TRAIN_DAYS = 365
CALIBRATION_DAYS = 90
TEST_MONTHS = 6
SEED = 41


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_feature_module():
    sys.path.insert(0, str(ROOT / "src"))
    spec = importlib.util.spec_from_file_location("prior_directional_features", FEATURE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load existing feature builder: {FEATURE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if EVENT_FILE.exists():
        module.EVENTS = pd.read_csv(EVENT_FILE)
    else:
        module.EVENTS = pd.DataFrame(columns=["release_utc", "event"])
    return module


def load_raw() -> pd.DataFrame:
    raw = pd.read_parquet(DATA).sort_index()
    raw = raw.loc[~raw.index.duplicated()].copy()
    raw.index = pd.DatetimeIndex(raw.index)
    if raw.index.tz is None:
        raw.index = raw.index.tz_localize("UTC")
    else:
        raw.index = raw.index.tz_convert("UTC")
    for col in ("open", "high", "low", "close", "volume", "spread"):
        if col in raw:
            raw[col] = pd.to_numeric(raw[col], errors="coerce")
    return raw.dropna(subset=["open", "high", "low", "close"])


def load_structure_states(raw: pd.DataFrame, engine_module):
    """Reuse prior validated states only when both input hashes still match."""
    cached_manifest = json.loads(OLD_MANIFEST.read_text(encoding="utf-8")) if OLD_MANIFEST.exists() else {}
    can_reuse = (
        cached_manifest.get("dataset", {}).get("sha256") == sha(DATA)
        and cached_manifest.get("implementation_hashes", {}).get("structure_engine_sha256") == sha(ENGINE)
    )
    states = {}
    for tf, minutes in (("M1", 1), ("M5", 5), ("M15", 15)):
        cache = RESULTS / f"structure_state_{tf}.parquet"
        if can_reuse and cache.exists():
            state = pd.read_parquet(cache)
            state.index = pd.DatetimeIndex(state.index)
        else:
            bars = engine_module.resample_m1(raw, minutes)
            state, _ = engine_module.structure(bars, tf, k=1.0)
            state.to_parquet(cache, compression="zstd")
        states[tf] = state
    return states, can_reuse


def feature_cache_key() -> str:
    return hashlib.sha256("|".join((sha(DATA), sha(FEATURE_SCRIPT), sha(ENGINE), sha(EVENT_FILE) if EVENT_FILE.exists() else "no-events")).encode()).hexdigest()


def load_or_build_features(raw: pd.DataFrame, builder, tf: str, cache_dir: Path, key: str):
    base_path = cache_dir / f"base_{tf}.parquet"
    feat_path = cache_dir / f"features_{tf}.parquet"
    meta_path = cache_dir / "cache_manifest.json"
    valid_cache = False
    if meta_path.exists() and base_path.exists() and feat_path.exists():
        try:
            valid_cache = json.loads(meta_path.read_text(encoding="utf-8")).get("key") == key
        except (OSError, json.JSONDecodeError):
            valid_cache = False
    if valid_cache:
        print(f"features cache {tf}", flush=True)
        return pd.read_parquet(base_path), pd.read_parquet(feat_path)
    print(f"build features {tf}", flush=True)
    base, features = builder.make_features(raw, tf)
    cache_dir.mkdir(parents=True, exist_ok=True)
    base.to_parquet(base_path, compression="zstd")
    features.to_parquet(feat_path, compression="zstd")
    meta_path.write_text(json.dumps({"key": key}, indent=2), encoding="utf-8")
    return base, features


def first_passage(raw_index_ns, raw_open, raw_high, raw_low, raw_close,
                  entry_idx, entry_price, stop_price, side, tp, horizon):
    """First passage over available M1 bars; ambiguous OHLC touches stop-first.

    Market closures have no candles, so the horizon counts available M1 bars.
    Reopening gaps are checked at the next available open. Only a data tail too
    short for the full horizon is censored; no barrier by the horizon is a
    negative timeout for the horizon-bounded target event.
    """
    n = len(entry_idx)
    result = np.full(n, -1, dtype=np.int8)
    outcome = np.full(n, "tail_censored", dtype="U16")
    exit_idx = np.full(n, -1, dtype=np.int32)
    exit_price = np.full(n, np.nan, dtype=np.float64)
    active = np.ones(n, dtype=bool)
    target = entry_price + side * tp
    for k in range(horizon):
        ix = entry_idx + k
        in_range = ix < len(raw_index_ns)
        active &= in_range
        if not active.any():
            break
        candidates = np.flatnonzero(active)
        ii = ix[candidates]
        long = side[candidates] == 1
        stop_open = np.where(long, raw_open[ii] <= stop_price[candidates], raw_open[ii] >= stop_price[candidates])
        target_open = np.where(long, raw_open[ii] >= target[candidates], raw_open[ii] <= target[candidates])
        stop_touch = np.where(long, raw_low[ii] <= stop_price[candidates], raw_high[ii] >= stop_price[candidates])
        target_touch = np.where(long, raw_high[ii] >= target[candidates], raw_low[ii] <= target[candidates])
        # Opening-gap events happen before the candle's later high/low path.
        stop_wins = stop_open | (~target_open & stop_touch & target_touch)
        target_wins = ~stop_wins & (target_open | target_touch)
        done = stop_wins | target_wins
        if done.any():
            local = candidates[done]
            is_target = target_wins[done]
            result[local] = is_target.astype(np.int8)
            exit_idx[local] = ii[done]
            gap_stop = stop_open[done]
            outcome[local] = np.where(is_target, "target", np.where(gap_stop, "stop_gap", "structural_stop"))
            exit_price[local] = np.where(is_target, target[local], np.where(gap_stop, raw_open[ii[done]], stop_price[local]))
            active[local] = False
    timeouts = np.flatnonzero(active)
    if len(timeouts):
        ix = entry_idx[timeouts] + horizon - 1
        complete = ix < len(raw_index_ns)
        ix_valid = timeouts[complete]
        ix = ix[complete]
        result[ix_valid] = 0
        outcome[ix_valid] = "timeout"
        exit_idx[ix_valid] = ix
        exit_price[ix_valid] = raw_close[ix]
    return result, outcome, exit_idx, exit_price


def event_metrics(y, p):
    y = np.asarray(y, dtype=np.int8)
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    bins = np.linspace(0.0, 1.0, 11)
    ece = 0.0
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (p >= lo) & ((p < hi) if hi < 1 else (p <= hi))
        if m.any():
            ece += float(m.mean() * abs(y[m].mean() - p[m].mean()))
    try:
        auc = float(roc_auc_score(y, p)) if np.unique(y).size > 1 else None
    except ValueError:
        auc = None
    return {
        "rows": int(len(y)), "target_rate": float(y.mean()) if len(y) else None,
        "mean_probability": float(p.mean()) if len(p) else None,
        "auc": auc,
        "brier": float(brier_score_loss(y, p)) if len(y) else None,
        "log_loss": float(log_loss(y, p, labels=[0, 1])) if len(y) else None,
        "ece_10_equal_width": float(ece) if len(y) else None,
    }


def fit_and_predict(x, y, row_times, row_meta, tf: str, tp: float, horizon: int):
    """Rolling 365d train; last 90d time-calibration; next 6mo test."""
    first = pd.Timestamp(row_times.min()) + pd.Timedelta(days=TRAIN_DAYS)
    fold = pd.Timestamp(first.year, first.month, 1, tz=first.tz)
    if fold < first:
        fold += pd.offsets.MonthBegin(1)
    end_data = pd.Timestamp(row_times.max()) + pd.Timedelta(nanoseconds=1)
    label_end_times = pd.DatetimeIndex(row_meta["label_exit_time"])
    outputs, metrics, gains = [], [], []
    fold_num = 0
    while fold < end_data:
        fold_num += 1
        next_fold = min(fold + pd.DateOffset(months=TEST_MONTHS), end_data)
        cal_start = fold - pd.Timedelta(days=CALIBRATION_DAYS)
        train_mask = ((row_times >= fold - pd.Timedelta(days=TRAIN_DAYS))
                      & (row_times < cal_start) & (label_end_times < cal_start))
        cal_mask = ((row_times >= cal_start) & (row_times < fold)
                    & (label_end_times < fold))
        test_mask = (row_times >= fold) & (row_times < next_fold)
        ti, ci, vi = np.flatnonzero(train_mask), np.flatnonzero(cal_mask), np.flatnonzero(test_mask)
        fold_record = {"tf": tf, "tp_price_units": tp, "fold": fold.isoformat(),
                       "test_end": next_fold.isoformat(), "n_train": int(len(ti)),
                       "n_calibration": int(len(ci)), "n_test": int(len(vi)),
                       "calibration": "time-ordered Platt scaling"}
        if len(ti) < 1000 or len(ci) < 200 or len(vi) < 100 or np.unique(y[ti]).size < 2 or np.unique(y[ci]).size < 2:
            metrics.append({**fold_record, "status": "insufficient_rows_or_class_support"})
            fold = next_fold
            continue
        model = LGBMClassifier(
            n_estimators=70, learning_rate=0.04, num_leaves=15,
            min_child_samples=100, subsample=0.85, subsample_freq=1,
            colsample_bytree=0.85, reg_lambda=8.0, verbosity=-1,
            random_state=SEED, n_jobs=4, deterministic=True, force_col_wise=True,
        )
        model.fit(x[ti], y[ti])
        p_cal_raw = model.predict_proba(x[ci])[:, 1]
        logit_cal = np.log(np.clip(p_cal_raw, 1e-5, 1 - 1e-5) / np.clip(1 - p_cal_raw, 1e-5, 1))
        calibrator = LogisticRegression(C=1e4, solver="lbfgs", random_state=SEED)
        calibrator.fit(logit_cal.reshape(-1, 1), y[ci])
        p_test_raw = model.predict_proba(x[vi])[:, 1]
        logit_test = np.log(np.clip(p_test_raw, 1e-5, 1 - 1e-5) / np.clip(1 - p_test_raw, 1e-5, 1))
        p_test = calibrator.predict_proba(logit_test.reshape(-1, 1))[:, 1]
        met = event_metrics(y[vi], p_test)
        metrics.append({**fold_record, **met, "status": "ok"})
        for row_i, raw_p, cal_p in zip(vi, p_test_raw, p_test):
            outputs.append({
                            "tf": tf, "tp_price_units": tp,
                            "decision_time": row_meta["decision_time"][row_i].isoformat(),
                            "entry_time": row_meta["entry_time"][row_i].isoformat(),
                            "entry_idx": int(row_meta["entry_idx"][row_i]),
                            "side": int(row_meta["side"][row_i]),
                            "entry_price": float(row_meta["entry_price"][row_i]),
                            "stop_price": float(row_meta["stop_price"][row_i]),
                            "stop_distance": float(row_meta["stop_distance"][row_i]),
                            "stop_distance_atr": float(row_meta["stop_distance_atr"][row_i]),
                            "outcome": str(row_meta["outcome"][row_i]),
                            "exit_idx": int(row_meta["exit_idx"][row_i]),
                            "label_exit_price": float(row_meta["label_exit_price"][row_i]),
                            "label_exit_time": row_meta["label_exit_time"][row_i].isoformat(),
                            "horizon_m1_bars": horizon,
                            "fold": fold.isoformat(),
                            "p_raw": float(raw_p), "p_calibrated": float(cal_p),
                            "target_hit": int(y[row_i])})
        importances = model.booster_.feature_importance("gain")
        denom = float(importances.sum()) or 1.0
        gains.extend({"tf": tf, "tp_price_units": tp, "fold": fold.isoformat(),
                      "feature": name, "gain_share": float(value / denom)}
                     for name, value in zip(FEATURE_COLUMNS[tf], importances))
        fold = next_fold
    return outputs, metrics, gains


FEATURE_COLUMNS: dict[str, list[str]] = {}


def simulate_fixed(raw_arrays, entry_idx: int, entry: float, side: int, tp: float, sl: float, horizon: int):
    _, op, hi, lo, cl = raw_arrays
    stop = entry - side * sl
    target = entry + side * tp
    for k in range(horizon):
        j = entry_idx + k
        if j >= len(op):
            return None
        stop_open = op[j] <= stop if side == 1 else op[j] >= stop
        target_open = op[j] >= target if side == 1 else op[j] <= target
        if stop_open:
            return j, float(op[j]), "stop_gap"
        if target_open:
            return j, target, "target"
        stop_hit = lo[j] <= stop if side == 1 else hi[j] >= stop
        target_hit = hi[j] >= target if side == 1 else lo[j] <= target
        if stop_hit and target_hit:
            return j, stop, "stop_same_bar"
        if stop_hit:
            return j, stop, "stop"
        if target_hit:
            return j, target, "target"
    return entry_idx + horizon - 1, float(cl[entry_idx + horizon - 1]), "timeout"


def pnl_for(entry: float, exit_price: float, side: int, lots: float):
    gross = side * (exit_price - entry) * CONTRACT_OZ * lots
    cost = COST_PRICE * CONTRACT_OZ * lots
    return gross - cost, gross, cost


def policy_backtest(predictions: pd.DataFrame, raw: pd.DataFrame, tf: str, tp: float):
    if predictions.empty:
        return [], []
    eligible = predictions.copy()
    eligible["stop_distance"] = np.where(eligible.side.eq(1), eligible.entry_price - eligible.stop_price,
                                         eligible.stop_price - eligible.entry_price)
    eligible["ev_price"] = (eligible.p_calibrated * tp
                             - (1 - eligible.p_calibrated) * eligible.stop_distance
                             - COST_PRICE)
    eligible = eligible[(eligible.stop_distance > 0) & (eligible.ev_price > 0)]
    if eligible.empty:
        return [], []
    chosen = eligible.sort_values(["entry_idx", "ev_price"], ascending=[True, False]).drop_duplicates("entry_idx")
    raw_arrays = (raw.index.asi8, raw.open.to_numpy(float), raw.high.to_numpy(float),
                  raw.low.to_numpy(float), raw.close.to_numpy(float))
    trades = []
    for r in chosen.sort_values("entry_idx").itertuples(index=False):
        structural = (int(r.exit_idx), float(r.label_exit_price), str(r.outcome))
        fixed = simulate_fixed(raw_arrays, int(r.entry_idx), float(r.entry_price), int(r.side), tp,
                               FIXED_SL, TF_HORIZON[tf])
        if fixed is None:
            continue
        fixed_exit_idx, fixed_exit_price, fixed_reason = fixed
        # Equal entry cohort: require the next trade to start after both exits.
        trades.append({"tf": tf, "tp_price_units": tp, "decision_time": r.decision_time,
                       "entry_time": r.entry_time, "entry_idx": int(r.entry_idx), "side": int(r.side),
                       "p_calibrated": float(r.p_calibrated), "estimated_edge_price": float(r.ev_price),
                       "entry_price": float(r.entry_price), "structural_stop": float(r.stop_price),
                       "structural_stop_distance": float(r.stop_distance),
                       "structural_exit_idx": structural[0], "structural_exit_price": structural[1],
                       "structural_reason": structural[2], "fixed_exit_idx": fixed_exit_idx,
                       "fixed_exit_price": fixed_exit_price, "fixed_reason": fixed_reason,
                       "common_exit_idx": max(structural[0], fixed_exit_idx)})
    accepted, last_end = [], -1
    for tr in trades:
        if tr["entry_idx"] <= last_end:
            continue
        accepted.append(tr)
        last_end = tr["common_exit_idx"]
    summaries = []
    for variant in ("structural", "fixed"):
        eq = INITIAL_EQUITY
        peak = INITIAL_EQUITY
        max_dd = 0.0
        pnls, costs = [], []
        wins = target_hits = low_lot_skips = 0
        locked_day = locked_week = None
        day_key = week_key = None
        day_start = week_start = INITIAL_EQUITY
        halted = False
        for tr in accepted:
            dt = pd.Timestamp(tr["entry_time"])
            current_day = dt.date()
            iso_week = dt.isocalendar()
            current_week = (iso_week.year, iso_week.week)
            if current_day != day_key:
                day_key, day_start, locked_day = current_day, eq, None
            if current_week != week_key:
                week_key, week_start, locked_week = current_week, eq, None
            if halted or locked_day == current_day or locked_week == current_week:
                continue
            stop_distance = tr["structural_stop_distance"] if variant == "structural" else FIXED_SL
            risk_per_lot = (stop_distance + COST_PRICE) * CONTRACT_OZ
            lots = math.floor((INITIAL_EQUITY * RISK_FRACTION) / risk_per_lot / LOT_STEP + 1e-9) * LOT_STEP
            if lots <= 0:
                low_lot_skips += 1
                continue
            tr[f"lots_{variant}"] = lots
            exit_price = tr[f"{variant}_exit_price"]
            exit_reason = tr[f"{variant}_reason"]
            p, gross, cost = pnl_for(tr["entry_price"], exit_price, tr["side"], lots)
            eq += p
            peak = max(peak, eq)
            max_dd = max(max_dd, (peak - eq) / peak)
            pnls.append(p); costs.append(cost)
            wins += int(p > 0)
            target_hits += int(exit_reason == "target")
            tr[f"net_pnl_{variant}"] = p
            if eq <= peak * (1 - PEAK_DRAWDOWN_LIMIT):
                halted = True
            elif eq <= day_start * (1 - DAILY_LOSS_LIMIT):
                locked_day = current_day
            elif eq <= week_start * (1 - WEEKLY_LOSS_LIMIT):
                locked_week = current_week
        x = np.asarray(pnls, dtype=float)
        gross_profit = float(x[x > 0].sum()) if len(x) else 0.0
        gross_loss = float(-x[x < 0].sum()) if len(x) else 0.0
        summaries.append({"tf": tf, "tp_price_units": tp, "variant": variant,
                          "candidate_signals": int(len(trades)), "common_nonoverlap_signals": int(len(accepted)),
                          "executed_trades": int(len(x)), "win_rate_net": float(wins / len(x)) if len(x) else None,
                          "target_hit_rate": float(target_hits / len(x)) if len(x) else None,
                          "net_profit": float(x.sum()) if len(x) else 0.0,
                          "expectancy_per_trade": float(x.mean()) if len(x) else None,
                          "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
                          "max_drawdown_pct": float(max_dd), "modeled_costs": float(sum(costs)),
                          "risk_fraction_of_starting_equity": RISK_FRACTION, "initial_equity": INITIAL_EQUITY,
                          "daily_loss_limit_pct": DAILY_LOSS_LIMIT, "weekly_loss_limit_pct": WEEKLY_LOSS_LIMIT,
                          "peak_drawdown_halt_pct": PEAK_DRAWDOWN_LIMIT,
                          "halted_at_peak_drawdown": bool(halted), "low_lot_skips": int(low_lot_skips)})
    return accepted, summaries


def refresh_policy_from_oos_files():
    """Re-run execution/risk accounting without refitting cached OOS models."""
    raw = load_raw()
    all_trades, all_summaries = [], []
    for tf in ("M1", "M5", "M15"):
        for tp in TARGETS:
            token = f"{tp:g}".replace(".", "p")
            path = OUT / f"oos_{tf}_tp{token}.parquet"
            if not path.exists():
                continue
            pred = pd.read_parquet(path)
            trades, summaries = policy_backtest(pred, raw, tf, tp)
            all_trades.extend(trades)
            all_summaries.extend(summaries)
    pd.DataFrame(all_summaries).to_csv(OUT / "policy_summary.csv", index=False)
    pd.DataFrame(all_trades).to_csv(OUT / "policy_trade_log.csv", index=False)
    manifest_path = OUT / "research_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["policy_simulation"] = {
        "sizing": "fixed 0.25% of starting equity risk per trade with 100 oz/lot and 0.01 lot step/minimum",
        "overlap": "paired stop comparison uses the same selected entry set and skips until the later exit of fixed and structural variants",
        "limits": {"daily_loss_pct": DAILY_LOSS_LIMIT, "weekly_loss_pct": WEEKLY_LOSS_LIMIT,
                   "peak_drawdown_halt_pct": PEAK_DRAWDOWN_LIMIT},
        "metrics": "net win rate, target hit rate, net expectancy, profit factor, equity close-to-close maximum drawdown",
        "limitations": "no compounding, intratrade mark-to-market DD, swaps, historical bid/ask path, or broker fill latency",
        "refreshed_from_saved_oos_predictions": True,
    }
    manifest["status"] = "completed exploratory target-before-structure research; risk-controlled policy accounting refreshed; research only"
    manifest["research_runner"] = {"script": str(Path(__file__).relative_to(ROOT)), "sha256": sha(Path(__file__))}
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"policy": all_summaries, "saved_oos_files_reused": True}, indent=2, default=str), flush=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cache_dir = OUT / "feature_cache"
    builder = load_feature_module()
    # Load the sibling structure implementation explicitly so the run hashes the
    # same source that created the prior protected-level states.
    spec = importlib.util.spec_from_file_location("target_structure_engine", ENGINE)
    engine = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(engine)
    validation = engine.validate_causality_and_semantics()
    if not all(validation.values()):
        raise RuntimeError(f"Causal structure validation failed: {validation}")
    raw = load_raw()
    states, reused_states = load_structure_states(raw, engine)
    raw_index_ns = raw.index.asi8
    raw_open, raw_high, raw_low, raw_close = (raw[c].to_numpy(float) for c in ("open", "high", "low", "close"))
    key = feature_cache_key()
    coverage_rows, label_rows, oos_overall, oos_files, fold_rows, gain_rows = [], [], [], [], [], []
    policy_rows, policy_summaries = [], []
    all_fold_counts = {}
    for tf in ("M1", "M5", "M15"):
        base, features = load_or_build_features(raw, builder, tf, cache_dir, key)
        candidates, entry_idx = builder.samples_for(raw, base, features, tf)
        if not len(candidates):
            raise RuntimeError(f"No valid current-confluence samples for {tf}")
        state = states[tf]
        state_pos = state.index.searchsorted(base.index[candidates], side="right") - 1
        ok_state = state_pos >= 0
        candidates, entry_idx, state_pos = candidates[ok_state], entry_idx[ok_state], state_pos[ok_state]
        s = state.iloc[state_pos]
        entry_price = raw_open[entry_idx]
        atr = pd.to_numeric(s.atr14, errors="coerce").to_numpy(float)
        protected_low = pd.to_numeric(s.protected_low, errors="coerce").to_numpy(float)
        protected_high = pd.to_numeric(s.protected_high, errors="coerce").to_numpy(float)
        latest_low = pd.to_numeric(s.latest_swing_low, errors="coerce").to_numpy(float)
        latest_high = pd.to_numeric(s.latest_swing_high, errors="coerce").to_numpy(float)
        trend = pd.to_numeric(s.trend, errors="coerce").fillna(0).to_numpy(float)
        structural_common = pd.DataFrame({
            "structure_trend": trend,
            "structure_range_compressed": s.range_compressed.astype(float).to_numpy(),
            "structure_mixed_swings": s.mixed_swings.astype(float).to_numpy(),
            "structure_consolidation": s.consolidation.astype(float).to_numpy(),
            "structure_atr14": atr,
        }, index=base.index[candidates])
        feature_names = builder.feature_groups(features.columns, tf)["all"]
        feature_names = list(dict.fromkeys(feature_names + list(structural_common.columns) +
                                          ["structure_stop_distance_atr", "structure_swing_distance_atr", "structure_trend_aligned"]))
        FEATURE_COLUMNS[tf] = feature_names
        x_base = features.iloc[candidates][builder.feature_groups(features.columns, tf)["all"]].to_numpy(np.float32)
        struct_matrix = structural_common.to_numpy(np.float32)
        candidate_n = len(candidates)
        side = np.r_[np.ones(candidate_n, dtype=np.int8), -np.ones(candidate_n, dtype=np.int8)]
        candidate_local = np.r_[np.arange(candidate_n), np.arange(candidate_n)]
        entries = entry_price[candidate_local]
        stops = np.r_[protected_low, protected_high]
        side_atr = atr[candidate_local]
        swing = np.r_[latest_low, latest_high]
        stop_distance = side * (entries - stops)
        swing_distance = side * (entries - swing)
        valid_side = np.isfinite(stops) & (stop_distance > 0) & np.isfinite(side_atr) & (side_atr > 0)
        row_times_all = base.index[candidates].append(base.index[candidates])
        row_entry_idx_all = np.r_[entry_idx, entry_idx]
        row_side_all = side
        row_candidate_all = candidate_local
        rows = np.flatnonzero(valid_side)
        row_times = pd.DatetimeIndex(row_times_all[rows])
        row_entry_idx = row_entry_idx_all[rows]
        row_side = row_side_all[rows]
        row_candidate = row_candidate_all[rows]
        row_stops = stops[rows]
        row_entries = entries[rows]
        row_distances = stop_distance[rows]
        row_atr = side_atr[rows]
        row_entry_times = raw.index[row_entry_idx]
        x = np.empty((len(rows), x_base.shape[1] + struct_matrix.shape[1] + 3), dtype=np.float32)
        x[:, :x_base.shape[1]] = x_base[row_candidate]
        x[:, x_base.shape[1]:x_base.shape[1] + struct_matrix.shape[1]] = struct_matrix[row_candidate]
        x[:, -3] = (row_distances / row_atr).astype(np.float32)
        x[:, -2] = (swing_distance[rows] / row_atr).astype(np.float32)
        x[:, -1] = (trend[row_candidate] * row_side).astype(np.float32)
        del x_base, struct_matrix
        for direction, level_arr in ((1, protected_low), (-1, protected_high)):
            distance = direction * (entry_price - level_arr)
            ok = np.isfinite(level_arr) & (distance > 0) & np.isfinite(atr) & (atr > 0)
            covered = distance[ok]
            coverage_rows.append({"tf": tf, "side": direction, "candidate_times": int(candidate_n),
                                  "valid_protected_stop_times": int(ok.sum()),
                                  "availability_pct": float(ok.mean()),
                                  "median_stop_distance": float(np.median(covered)) if len(covered) else None,
                                  "p90_stop_distance": float(np.quantile(covered, .90)) if len(covered) else None,
                                  "median_stop_distance_atr": float(np.median(covered / atr[ok])) if len(covered) else None})
        print(f"{tf}: candidates={candidate_n:,}, valid_directional_structure_rows={len(rows):,}", flush=True)
        all_fold_counts[tf] = 0
        for tp in TARGETS:
            y, outcome, exit_idx, exit_price = first_passage(
                raw_index_ns, raw_open, raw_high, raw_low, raw_close,
                row_entry_idx, row_entries, row_stops, row_side, tp, TF_HORIZON[tf])
            label_ok = y >= 0
            label_rows.append({"tf": tf, "tp_price_units": tp, "candidate_rows": int(candidate_n),
                               "directional_structure_rows": int(len(rows)),
                               "valid_label_rows": int(label_ok.sum()), "tail_censored_rows": int((~label_ok).sum()),
                               "target_first": int(np.sum(outcome == "target")),
                               "structural_stop_first": int(np.sum(np.isin(outcome, ["structural_stop", "stop_gap"]))),
                               "stop_gap_first": int(np.sum(outcome == "stop_gap")),
                               "timeout": int(np.sum(outcome == "timeout")),
                               "target_rate": float(y[label_ok].mean()) if label_ok.any() else None,
                               "mean_stop_distance": float(row_distances[label_ok].mean()) if label_ok.any() else None,
                               "median_stop_distance": float(np.median(row_distances[label_ok])) if label_ok.any() else None})
            if not label_ok.any():
                continue
            valid_rows = np.flatnonzero(label_ok)
            row_meta = {
                "decision_time": row_times[valid_rows],
                "entry_time": pd.DatetimeIndex(row_entry_times[valid_rows]),
                "entry_idx": row_entry_idx[valid_rows],
                "side": row_side[valid_rows],
                "entry_price": row_entries[valid_rows],
                "stop_price": row_stops[valid_rows],
                "stop_distance": row_distances[valid_rows],
                "stop_distance_atr": row_distances[valid_rows] / row_atr[valid_rows],
                "outcome": outcome[valid_rows],
                "exit_idx": exit_idx[valid_rows],
                "label_exit_price": exit_price[valid_rows],
                "label_exit_time": pd.DatetimeIndex(raw.index[exit_idx[valid_rows]]),
            }
            predictions, fold_metrics, gains = fit_and_predict(
                x[valid_rows], y[valid_rows], row_times[valid_rows], row_meta, tf, tp, TF_HORIZON[tf])
            fold_rows.extend(fold_metrics)
            gain_rows.extend(gains)
            all_fold_counts[tf] += sum(m.get("status") == "ok" for m in fold_metrics)
            pred = pd.DataFrame(predictions)
            if pred.empty:
                continue
            token = f"{tp:g}".replace(".", "p")
            pred_path = OUT / f"oos_{tf}_tp{token}.parquet"
            pred.to_parquet(pred_path, index=False, compression="zstd")
            oos_files.append(pred_path.name)
            # Per-fold test probabilities and structural-first/trade outcomes are
            # retained for audit, calibration review, and a reproducible policy.
            tf_tp = pred[(pred.tf == tf) & (pred.tp_price_units == tp)].copy()
            chosen, summaries = policy_backtest(tf_tp, raw, tf, tp)
            policy_rows.extend(chosen)
            policy_summaries.extend(summaries)
            metric = event_metrics(tf_tp.target_hit.to_numpy(int), tf_tp.p_calibrated.to_numpy(float))
            oos_overall.append({"tf": tf, "tp_price_units": tp, **metric})
            print(f"  TP={tp:g}: OOS rows={len(pred):,}, folds={sum(m.get('status') == 'ok' for m in fold_metrics)}, AUC={metric['auc']}", flush=True)

    coverage = pd.DataFrame(coverage_rows)
    labels = pd.DataFrame(label_rows)
    folds = pd.DataFrame(fold_rows)
    policy_trades, policy = pd.DataFrame(policy_rows), pd.DataFrame(policy_summaries)
    gains = pd.DataFrame(gain_rows)
    coverage.to_csv(OUT / "structure_coverage.csv", index=False)
    labels.to_csv(OUT / "label_summary.csv", index=False)
    folds.to_csv(OUT / "walkforward_calibration_metrics.csv", index=False)
    if oos_overall:
        pd.DataFrame(oos_overall).to_csv(OUT / "oos_probability_summary.csv", index=False)
    policy.to_csv(OUT / "policy_summary.csv", index=False)
    policy_trades.to_csv(OUT / "policy_trade_log.csv", index=False)
    gains.to_csv(OUT / "feature_importance.csv", index=False)

    old_report_path = RESULTS / "STOP_COMPARISON_REPORT.md"
    inspected_start = raw.index[-1] - pd.Timedelta(days=180)
    manifest = {
        "status": "completed exploratory target-before-structure research; research only; no bot or MT5 changes",
        "question": "Estimate P(fixed TP reached before the protected stop known at entry, within the timeframe horizon | current confluences and structure state).",
        "dataset": {"file": str(DATA.relative_to(ROOT)), "sha256": sha(DATA), "rows": int(len(raw)),
                    "start": raw.index[0].isoformat(), "end": raw.index[-1].isoformat(), "resolution": "M1 OHLC and broker spread/tick volume"},
        "feature_source": {"script": str(FEATURE_SCRIPT.relative_to(ROOT)), "sha256": sha(FEATURE_SCRIPT),
                           "feature_family": "existing all-feature group, including candle, session/news, completed HTF context, levels, sweeps, FVG/order-block proxies, tick-volume profile, and confluence scores",
                           "additional_features": ["entry-time causal structure trend/range flags", "protected stop distance in ATR", "confirmed swing distance in ATR", "direction-aligned structure trend"]},
        "structure": {"source": str(ENGINE.relative_to(ROOT)), "sha256": sha(ENGINE), "method": "existing ATR(14) directional-change engine, k=1.0; protected low for long and protected high for short frozen at decision close", "cached_states_reused": bool(reused_states), "causal_semantic_checks": validation},
        "sample": {"candidate_schedule": "same existing sampling: every second completed M1 close; every completed M5/M15 close", "decision": "completed timeframe bar close", "entry": "next M1 open", "structure_availability": "report denominator includes all feature-valid candidate times x both directions; invalid/wrong-side protected levels are excluded only from structural label cohort", "horizons_m1_bars": TF_HORIZON},
        "targets_price_units": list(TARGETS),
        "labels": {"positive": "TP first within the timeframe's M1-bar horizon", "negative": "protected stop first or no barrier before horizon timeout", "timeouts": "negative for the explicitly horizon-bounded target event; separately identified in labels", "market_closures": "horizon counts available M1 bars; reopening gaps are evaluated at the next observed M1 open", "tail": "right-censored when the dataset does not contain the full horizon", "same_m1_bar_tp_and_stop": "stop first", "entry_stop": "stop frozen from causal structure known at decision close; entry/reopening open may gap through it and count as stop"},
        "execution_costs": {"round_trip_spread_price": SPREAD_PRICE, "slippage_price_each_side": SLIPPAGE_PER_SIDE,
                            "commission_per_lot_round_turn": COMMISSION_PER_LOT, "contract_oz_per_lot": CONTRACT_OZ,
                            "cost_price_equivalent": COST_PRICE, "tp_slippage": "target fills at limit price", "stop_gap": "fills at M1 open"},
        "model": {"estimator": "LightGBM binary classifier", "calibration": "time-ordered Platt logistic calibration on the latest 90 days inside each 365-day training window", "walkforward": "rolling 365-day history; latest 90 days used only for calibration; each training/calibration label must have exited before the next window boundary; next 6 calendar months test; windows do not overlap", "hyperparameters": {"estimators": 70, "learning_rate": 0.04, "num_leaves": 15, "min_child_samples": 100}, "decision_policy": "choose the side with highest calibrated probability only when conservative target-minus-full-stop-minus-cost expected edge is positive; no empirical threshold sweep"},
        "policy_simulation": {"sizing": "fixed 0.25% of starting equity risk per trade with 100 oz/lot and 0.01 lot step/minimum", "overlap": "paired stop comparison uses same selected entry set and skips until the later exit of fixed and structural variants", "limits": {"daily_loss_pct": DAILY_LOSS_LIMIT, "weekly_loss_pct": WEEKLY_LOSS_LIMIT, "peak_drawdown_halt_pct": PEAK_DRAWDOWN_LIMIT}, "metrics": "net win rate, target hit rate, net expectancy, profit factor, equity close-to-close maximum drawdown", "limitations": "no compounding, intratrade mark-to-market DD, swaps, historical bid/ask path, or broker fill latency"},
        "prior_inspected_data": {"known_inspected_window_start": inspected_start.isoformat(), "end": raw.index[-1].isoformat(), "reason": "prior research report explicitly inspected the latest six months; all walk-forward outputs here are exploratory, not a fresh untouched holdout"},
        "baseline_context": {"paired_stop_report": str(old_report_path.relative_to(ROOT)), "legacy_full_selected_trade_report": str((INPUT / "research_report.json").relative_to(ROOT)), "fixed_stop_control_scope": "paired execution counterfactual: same entries selected by structural-model probabilities and structural risk filter; not a separately trained fixed-stop strategy or full original baseline", "production_signal": "none exists in Python bot per ENTRY_SIGNAL_AUDIT.md; new classifier is research-only"},
        "fast_mode_batch_scope": {"reference_project_config": "C:/Users/hp/Downloads/xauusdm-wickless-structure-trading-bot/config.yaml", "configured_orders_per_signal": "6-7", "configured_spacing_seconds": "1-4", "configured_total_batch_risk_pct": 0.5, "configured_open_trade_cap": 12, "runtime_status": "not implemented in sibling src/backtest; engine accepts one OrderRequest per timestamp", "test_status": "not tested; M1 OHLC cannot resolve 1-4 second fill sequence or batch margin/latency", "risk_note": "0.5% must be shared by the batch; assigning it to each of six legs would risk 3% per signal before gaps"},
        "folds_successful_by_tf": all_fold_counts,
        "outputs": ["structure_coverage.csv", "label_summary.csv", "walkforward_calibration_metrics.csv", "oos_probability_summary.csv", *oos_files, "policy_summary.csv", "policy_trade_log.csv", "feature_importance.csv"],
        "research_runner": {"script": str(Path(__file__).relative_to(ROOT)), "sha256": sha(Path(__file__))},
        "warnings": ["TP values are XAUUSD price units, not account-currency profit amounts; 100 oz/lot is used for accounting.", "OHLC data cannot reveal intraminute path or variable executable bid/ask; ambiguous TP/SL touches are stops.", "The structure method is an OHLC causal approximation, not an exact reproduction of the Pine plotting implementation.", "The fast-mode batch runner is not implemented in the sibling bot and cannot be tested from M1 OHLC bars.", "Low outcome counts, overlapping market regimes, prior inspection of this dataset, and threshold calibration mean no profitability claim or deployment recommendation is supported without new untouched data."],
    }
    (OUT / "research_manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"output": str(OUT.relative_to(ROOT)), "labels": label_rows,
                      "oos_metrics": oos_overall, "policy": policy_summaries,
                      "folds_successful_by_tf": all_fold_counts}, indent=2, default=str), flush=True)


if __name__ == "__main__":
    if "--policy-only" in sys.argv[1:]:
        refresh_policy_from_oos_files()
    else:
        main()
