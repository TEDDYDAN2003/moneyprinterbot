from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_config
from bot.mt5_adapter import MT5Adapter


def main() -> None:
    parser = argparse.ArgumentParser(description="Export MT5 M1 candles to CSV for offline backtesting")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--bars", type=int, default=20000, help="Number of M1 bars to export")
    parser.add_argument("--out", default="data/xauusdm_m1.csv", help="Output CSV path")
    args = parser.parse_args()

    cfg = load_config(args.config)
    adapter = MT5Adapter(cfg.mt5)
    if not adapter.connect():
        raise SystemExit("Failed to connect to MT5 Desktop terminal.")

    try:
        candles = adapter.get_m1_candles(cfg.mt5.symbol, args.bars)
    finally:
        adapter.disconnect()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time", "open", "high", "low", "close", "tick_volume", "spread"])
        for c in candles:
            writer.writerow([c.time.isoformat(), c.open, c.high, c.low, c.close, c.tick_volume, c.spread])

    print(f"Exported {len(candles)} M1 bars for {cfg.mt5.symbol} to {out_path}")


if __name__ == "__main__":
    main()
