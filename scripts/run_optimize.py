from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.backtester import generate_synthetic_xau_m1, load_candles_from_csv
from bot.config import dump_yaml_dict, load_config, load_yaml_file
from bot.optimizer import StrategyOptimizer


def main() -> None:
    parser = argparse.ArgumentParser(description="Optimize XAUUSDm Wickless Structure & Dollar-Exit Parameters")
    parser.add_argument("--config", default="config.yaml", help="Base config.yaml path")
    parser.add_argument("--csv", default="", help="Historical M1 CSV path (leave empty for synthetic sample)")
    parser.add_argument("--from-mt5", action="store_true", help="Pull M1 bars directly from MT5 Desktop")
    parser.add_argument("--bars", type=int, default=3000, help="Bars count if fetching from MT5 or synthetic")
    parser.add_argument("--trials", type=int, default=25, help="Optimization trials")
    parser.add_argument("--balance", type=float, default=10000.0, help="Initial balance USD")
    parser.add_argument("--spread", type=float, default=25.0, help="Simulated spread in points")
    parser.add_argument("--out", default="config.optimized.yaml", help="Output path for best YAML config")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.from_mt5:
        from bot.mt5_adapter import MT5Adapter

        adapter = MT5Adapter(cfg.mt5)
        if not adapter.connect():
            raise SystemExit("Could not connect to MT5 Desktop terminal.")
        candles = adapter.get_m1_candles(cfg.mt5.symbol, args.bars)
        adapter.disconnect()
    elif args.csv:
        candles = load_candles_from_csv(args.csv)
    else:
        candles = generate_synthetic_xau_m1(num_bars=args.bars)

    opt = StrategyOptimizer(
        base_cfg=cfg,
        initial_balance=args.balance,
        default_spread_points=args.spread,
    )
    summary = opt.optimize(candles=candles, n_trials=args.trials)

    print("=" * 70)
    print(f"OPTIMIZATION COMPLETE ({summary.engine_used}, {len(candles)} M1 bars)")
    print("=" * 70)
    for rank, tr in enumerate(summary.top_trials, start=1):
        print(
            f"#{rank} Trial {tr.trial_id:2d} | Score={tr.score:7.2f} | "
            f"IS: Net=${tr.in_sample.net_profit_usd:+8.2f} PF={tr.in_sample.profit_factor:.2f} DD={tr.in_sample.max_drawdown_pct:.1f}% | "
            f"OOS: Net=${tr.out_of_sample.net_profit_usd:+8.2f} PF={tr.out_of_sample.profit_factor:.2f} DD={tr.out_of_sample.max_drawdown_pct:.1f}%"
        )
        print(f"    Params: {tr.params}")

    # Export best params merged into base YAML
    raw_yaml = load_yaml_file(Path(args.config))
    best_params = summary.best_trial.params
    raw_yaml.setdefault("strategy", {})
    raw_yaml.setdefault("dollar_exits", {})
    for k, v in best_params.items():
        if k in ("trade_tp_usd", "basket_tp_usd", "trade_sl_usd", "basket_sl_usd"):
            raw_yaml["dollar_exits"][k] = v
        else:
            raw_yaml["strategy"][k] = v

    Path(args.out).write_text(dump_yaml_dict(raw_yaml), encoding="utf-8")
    print(f"\nSaved best configuration to: {args.out}")


if __name__ == "__main__":
    main()
