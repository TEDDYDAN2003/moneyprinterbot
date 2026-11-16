from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field
from typing import Any

from bot.backtester import BacktestReport, EventBacktester
from bot.models import BacktestMetrics, BotConfig, Candle


@dataclass
class OptimizationTrialResult:
    trial_id: int
    score: float
    params: dict[str, Any]
    in_sample: BacktestMetrics
    out_of_sample: BacktestMetrics


@dataclass
class OptimizationSummary:
    best_trial: OptimizationTrialResult
    top_trials: list[OptimizationTrialResult] = field(default_factory=list)
    engine_used: str = "random_walkforward"


def _score_metrics(m: BacktestMetrics, min_trades: int = 8) -> float:
    if m.total_trades < min_trades:
        return -1000.0 + m.total_trades
    # Balanced risk-adjusted score: reward profit factor, net profit, and Sharpe while penalizing DD
    dd_penalty = max(1.0, m.max_drawdown_pct)
    return (m.net_profit_usd / 100.0) + (m.profit_factor * 4.0) + (m.sharpe_ratio * 1.5) - (dd_penalty * 0.8)


def apply_params_to_config(base_cfg: BotConfig, params: dict[str, Any]) -> BotConfig:
    cfg = copy.deepcopy(base_cfg)
    for key, val in params.items():
        if hasattr(cfg.strategy, key):
            setattr(cfg.strategy, key, val)
        elif hasattr(cfg.risk, key):
            setattr(cfg.risk, key, val)
        elif hasattr(cfg.dollar_exits, key):
            setattr(cfg.dollar_exits, key, val)
    return cfg


class StrategyOptimizer:
    """
    Walk-forward parameter optimizer supporting Optuna Bayesian TPE (when installed)
    with a pure-Python deterministic search fallback.
    """

    def __init__(
        self,
        base_cfg: BotConfig,
        initial_balance: float = 10000.0,
        train_ratio: float = 0.70,
        min_trades: int = 5,
        default_spread_points: float = 25.0,
    ) -> None:
        self.base_cfg = base_cfg
        self.initial_balance = initial_balance
        self.train_ratio = train_ratio
        self.min_trades = min_trades
        self.default_spread_points = default_spread_points

    def _evaluate_params(
        self,
        params: dict[str, Any],
        train_bars: list[Candle],
        test_bars: list[Candle],
    ) -> tuple[float, BacktestReport, BacktestReport]:
        cfg = apply_params_to_config(self.base_cfg, params)
        bt = EventBacktester(
            cfg,
            initial_balance=self.initial_balance,
            default_spread_points=self.default_spread_points,
        )
        in_rep = bt.run(train_bars)
        out_rep = bt.run(test_bars)

        in_score = _score_metrics(in_rep.metrics, min_trades=self.min_trades)
        out_score = _score_metrics(out_rep.metrics, min_trades=max(2, self.min_trades // 2))
        # Walk-forward objective: require strong performance on BOTH in-sample and out-of-sample
        combined_score = (0.45 * in_score) + (0.55 * out_score)
        return combined_score, in_rep, out_rep

    def optimize(
        self,
        candles: list[Candle],
        n_trials: int = 25,
        seed: int = 42,
        prefer_optuna: bool = True,
    ) -> OptimizationSummary:
        split_idx = max(80, int(len(candles) * self.train_ratio))
        train_bars = candles[:split_idx]
        test_bars = candles[split_idx:]
        if len(test_bars) < 50:
            train_bars = candles
            test_bars = candles

        trials: list[OptimizationTrialResult] = []
        engine_used = "stdlib_random_walkforward"

        optuna_mod = None
        if prefer_optuna:
            try:
                import optuna  # type: ignore

                optuna.logging.set_verbosity(optuna.logging.WARNING)
                optuna_mod = optuna
                engine_used = "optuna_tpe_walkforward"
            except ImportError:
                optuna_mod = None

        if optuna_mod is not None:
            sampler = optuna_mod.samplers.TPESampler(seed=seed)
            study = optuna_mod.create_study(direction="maximize", sampler=sampler)

            def objective(trial) -> float:
                params = {
                    "entry_mode": trial.suggest_categorical(
                        "entry_mode", ["retest_limit", "retest_rejection", "immediate"]
                    ),
                    "tol_ticks": trial.suggest_categorical("tol_ticks", [0.0, 1.0, 2.0, 5.0]),
                    "zone_pips": trial.suggest_float("zone_pips", 1.0, 10.0, step=1.0),
                    "retest_bars": trial.suggest_int("retest_bars", 4, 12, step=2),
                    "internal_len": trial.suggest_int("internal_len", 2, 5),
                    "rr_value": trial.suggest_float("rr_value", 1.0, 2.5, step=0.25),
                    "require_external_alignment": trial.suggest_categorical(
                        "require_external_alignment", [False, True]
                    ),
                    "block_in_consolidation": trial.suggest_categorical(
                        "block_in_consolidation", [False, True]
                    ),
                    "trade_tp_usd": trial.suggest_categorical("trade_tp_usd", [0.0, 2.5, 4.0, 6.0, 10.0]),
                    "trade_sl_usd": trial.suggest_categorical("trade_sl_usd", [0.0, 2.5, 4.0, 6.0, 10.0]),
                    "basket_tp_usd": trial.suggest_categorical("basket_tp_usd", [0.0]),
                    "basket_sl_usd": trial.suggest_categorical("basket_sl_usd", [0.0]),
                }
                score, in_rep, out_rep = self._evaluate_params(params, train_bars, test_bars)
                trials.append(
                    OptimizationTrialResult(
                        trial_id=trial.number,
                        score=round(score, 3),
                        params=params,
                        in_sample=in_rep.metrics,
                        out_of_sample=out_rep.metrics,
                    )
                )
                return score

            study.optimize(objective, n_trials=n_trials)
        else:
            rng = random.Random(seed)
            space = {
                "entry_mode": ["retest_limit", "retest_rejection", "immediate"],
                "tol_ticks": [0.0, 1.0, 2.0, 5.0],
                "zone_pips": [1.0, 2.0, 3.0, 4.0, 5.0, 7.0, 9.0],
                "retest_bars": [4, 6, 8, 10, 12],
                "internal_len": [2, 3, 4],
                "rr_value": [1.0, 1.25, 1.5, 2.0],
                "require_external_alignment": [False, True],
                "block_in_consolidation": [False, True],
                "trade_tp_usd": [0.0, 2.5, 4.0, 6.0, 10.0],
                "trade_sl_usd": [0.0, 2.5, 4.0, 6.0, 10.0],
                "basket_tp_usd": [0.0],
                "basket_sl_usd": [0.0],
            }
            for idx in range(n_trials):
                if idx == 0:
                    # Always evaluate user's current config baseline as trial 0
                    params = {
                        "entry_mode": self.base_cfg.strategy.entry_mode,
                        "tol_ticks": self.base_cfg.strategy.tol_ticks,
                        "zone_pips": self.base_cfg.strategy.zone_pips,
                        "retest_bars": self.base_cfg.strategy.retest_bars,
                        "internal_len": self.base_cfg.strategy.internal_len,
                        "rr_value": self.base_cfg.strategy.rr_value,
                        "require_external_alignment": self.base_cfg.strategy.require_external_alignment,
                        "block_in_consolidation": self.base_cfg.strategy.block_in_consolidation,
                        "trade_tp_usd": self.base_cfg.dollar_exits.trade_tp_usd,
                        "trade_sl_usd": self.base_cfg.dollar_exits.trade_sl_usd,
                        "basket_tp_usd": self.base_cfg.dollar_exits.basket_tp_usd,
                        "basket_sl_usd": self.base_cfg.dollar_exits.basket_sl_usd,
                    }
                else:
                    params = {k: rng.choice(v) for k, v in space.items()}

                score, in_rep, out_rep = self._evaluate_params(params, train_bars, test_bars)
                trials.append(
                    OptimizationTrialResult(
                        trial_id=idx,
                        score=round(score, 3),
                        params=params,
                        in_sample=in_rep.metrics,
                        out_of_sample=out_rep.metrics,
                    )
                )

        trials.sort(key=lambda t: t.score, reverse=True)
        return OptimizationSummary(
            best_trial=trials[0],
            top_trials=trials[:5],
            engine_used=engine_used,
        )
