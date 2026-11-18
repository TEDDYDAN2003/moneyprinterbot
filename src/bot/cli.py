from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import pandas as pd

from bot.config.loader import load_yaml
from bot.config.schema import validate_config
from bot.data.downloader import fetch_mt5_m1, load_ohlcv, save_ohlcv_parquet
from bot.features.pipeline import build_multitimeframe_features
from bot.labeling.triple_barrier import triple_barrier_labels
from bot.models.baseline import LogisticBaseline


def main() -> int:
    parser = argparse.ArgumentParser(prog="xauusd-bot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-config")
    validate.add_argument("--config", default="config.yaml")
    download = subparsers.add_parser("download")
    download.add_argument("--symbol", default="XAUUSDm")
    download.add_argument("--years", type=int, default=3)
    download.add_argument("--out", default="data/bars/xauusdm_m1.parquet")
    download.add_argument("--allow-partial", action="store_true")
    feature_parser = subparsers.add_parser("features")
    feature_parser.add_argument("--csv", required=True)
    feature_parser.add_argument("--out", required=True)
    label_parser = subparsers.add_parser("label")
    label_parser.add_argument("--features", required=True)
    label_parser.add_argument("--out", required=True)
    label_parser.add_argument("--horizon-bars", type=int, default=15)
    train_parser = subparsers.add_parser("train")
    train_parser.add_argument("--dataset", required=True)
    train_parser.add_argument("--out", required=True)
    demo = subparsers.add_parser("trade", aliases=["demo"], help="observe MT5; account orders require --arm-orders")
    demo.add_argument("--config", default="config.yaml")
    demo.add_argument("--timeframe", choices=("M1", "M5", "M15"), default="M1")
    demo.add_argument("--poll-seconds", type=float, default=2.0)
    demo.add_argument("--max-iterations", type=int)
    demo.add_argument("--arm-orders", "--arm-demo", dest="arm_orders", action="store_true",
                      help="allow orders on the currently connected MT5 account")
    trade_all = subparsers.add_parser("trade-all", help="run independent M1, M5 and M15 workers concurrently")
    trade_all.add_argument("--config", default="config.yaml")
    trade_all.add_argument("--poll-seconds", type=float, default=2.0)
    trade_all.add_argument("--max-iterations", type=int)
    trade_all.add_argument("--arm-orders", action="store_true",
                           help="allow orders on the currently connected MT5 account")
    args = parser.parse_args()
    if args.command == "validate-config":
        config = validate_config(load_yaml(Path(args.config)))
        print(json.dumps(config.model_dump(), indent=2, default=str))
        return 0
    if args.command in {"trade", "demo"}:
        config = load_yaml(Path(args.config))
        validate_config(config)
        from bot.execution.demo_runner import run_demo

        run_demo(
            config,
            args.timeframe,
            armed=args.arm_orders,
            poll_seconds=args.poll_seconds,
            max_iterations=args.max_iterations,
        )
        return 0
    if args.command == "trade-all":
        children = []
        for timeframe in ("M1", "M5", "M15"):
            command = [
                sys.executable, "-m", "bot.cli", "trade", "--config", args.config,
                "--timeframe", timeframe, "--poll-seconds", str(args.poll_seconds),
            ]
            if args.max_iterations is not None:
                command.extend(("--max-iterations", str(args.max_iterations)))
            if args.arm_orders:
                command.append("--arm-orders")
            children.append(subprocess.Popen(command))
        try:
            while any(child.poll() is None for child in children):
                failed = [child for child in children if child.poll() not in (None, 0)]
                if failed:
                    for child in children:
                        if child.poll() is None:
                            child.terminate()
                    return 1
                time.sleep(0.25)
        except KeyboardInterrupt:
            for child in children:
                if child.poll() is None:
                    child.terminate()
            for child in children:
                child.wait()
            return 130
        return max((child.returncode or 0) for child in children)
    if args.command == "features":
        bars = load_ohlcv(args.csv)
        build_multitimeframe_features(bars).to_parquet(args.out)
        return 0
    if args.command == "download":
        save_ohlcv_parquet(
            fetch_mt5_m1(args.symbol, args.years, allow_partial=args.allow_partial),
            args.out,
        )
        print(f"saved {args.out}")
        return 0
    if args.command == "label":
        frame = pd.read_parquet(args.features)
        labels = triple_barrier_labels(frame, args.horizon_bars, 0.8, 1.0)
        frame.assign(label=labels).to_parquet(args.out)
        return 0
    if args.command == "train":
        frame = pd.read_parquet(args.dataset).dropna(subset=["label"])
        excluded = {"label", "close"}
        columns = [column for column in frame.columns if column not in excluded]
        model = LogisticBaseline().fit(frame[columns].to_numpy(), frame["label"].to_numpy())
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        np.savez(output, weights=model.weights, mean=model.mean_, scale=model.scale_, columns=columns)
        return 0
    raise RuntimeError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
