from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.backtester import (
    EventBacktester,
    default_xau_spec,
    generate_synthetic_xau_m1,
    load_candles_from_csv,
)
from bot.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest XAUUSDm Wickless Structure Strategy")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML configuration")
    parser.add_argument("--csv", default="", help="Path to historical M1 OHLC CSV")
    parser.add_argument("--from-mt5", action="store_true", help="Fetch historical M1 bars directly from local MT5")
    parser.add_argument("--bars", type=int, default=5000, help="Number of bars to fetch/generate")
    parser.add_argument("--balance", type=float, default=10000.0, help="Starting account balance USD")
    parser.add_argument("--spread", type=float, default=25.0, help="Default simulated spread in points")
    args = parser.parse_args()

    cfg = load_config(args.config)
    spec = default_xau_spec(cfg.mt5.symbol)

    if args.from_mt5:
        from bot.mt5_adapter import MT5Adapter

        adapter = MT5Adapter(cfg.mt5)
        if not adapter.connect():
            raise SystemExit("Could not connect to MT5 Desktop terminal.")
        spec = adapter.get_symbol_spec(cfg.mt5.symbol)
        candles = adapter.get_m1_candles(cfg.mt5.symbol, args.bars)
        adapter.disconnect()
        source_label = f"MT5 Live History ({len(candles)} M1 bars)"
    elif args.csv:
        candles = load_candles_from_csv(args.csv)
        source_label = f"CSV {args.csv} ({len(candles)} M1 bars)"
    else:
        candles = generate_synthetic_xau_m1(num_bars=args.bars)
        source_label = f"Synthetic Gold Sample ({len(candles)} M1 bars)"

    bt = EventBacktester(cfg=cfg, spec=spec, initial_balance=args.balance, default_spread_points=args.spread)
    report = bt.run(candles)
    m = report.metrics

    print("=" * 64)
    print(f"BACKTEST REPORT — {cfg.mt5.symbol} | {source_label}")
    print("=" * 64)
    print(f"Strategy Mode     : {cfg.strategy.mode} ({cfg.strategy.entry_mode})")
    print(f"Dollar TP / SL    : +${cfg.dollar_exits.trade_tp_usd:.2f} / -${cfg.dollar_exits.trade_sl_usd:.2f} "
          f"(Basket: +${cfg.dollar_exits.basket_tp_usd:.2f} / -${cfg.dollar_exits.basket_sl_usd:.2f})")
    print(f"Total Trades      : {m.total_trades} (Wins: {m.wins} | Losses: {m.losses})")
    print(f"Win Rate          : {m.win_rate_pct:.2f}%")
    print(f"Net Profit        : ${m.net_profit_usd:+,.2f} ({m.return_pct:+.2f}%)")
    print(f"Profit Factor     : {m.profit_factor:.3f}")
    print(f"Expectancy / Trade: ${m.expectancy_usd:+.2f} | Avg R: {m.avg_r_multiple:+.3f}R (Total: {m.total_r:+.2f}R)")
    print(f"Max Drawdown      : ${m.max_drawdown_usd:,.2f} ({m.max_drawdown_pct:.2f}%)")
    print(f"Sharpe Ratio      : {m.sharpe_ratio:.3f}")
    print(f"Cancelled Setups  : {m.cancelled_setups}")
    print(f"Exit Breakdown    : {m.exit_breakdown}")
    print("=" * 64)


if __name__ == "__main__":
    main()
