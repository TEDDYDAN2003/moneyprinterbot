from __future__ import annotations

import argparse
import json
from pathlib import Path

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
    from bot.hull.cli import register as register_hull

    register_hull(subparsers)
    replay = subparsers.add_parser(
        "zones-replay", help="offline costed replay; never connects to MT5"
    )
    replay.add_argument("--config", default="config.yaml")
    replay.add_argument("--symbol", default="XAUUSD", help="key in zones.symbols")
    feed = replay.add_mutually_exclusive_group(required=True)
    feed.add_argument("--csv", help="minute-open bid OHLC CSV/MT5 export or parquet")
    feed.add_argument(
        "--ticks", nargs="+", help="chronological UTC timestamp,bid,ask CSV[.gz] files"
    )
    replay.add_argument(
        "--source-tz", default="UTC", help="zone of naive bar-export times"
    )
    replay.add_argument(
        "--start", help="inclusive UTC; earlier bars are signal warm-up only"
    )
    replay.add_argument("--end", help="exclusive UTC")
    replay.add_argument("--strategy", choices=("zones", "legacy"), default="zones")
    replay.add_argument(
        "--stop-fill",
        choices=("barrier", "first_quote"),
        default="barrier",
        help="barrier: ideal zero stop slippage; first_quote: allow gaps beyond stop",
    )
    replay.add_argument(
        "--spread-override",
        type=float,
        help="COUNTERFACTUAL fixed spread in price units",
    )
    replay.add_argument(
        "--decisions", action="store_true", help="write one audit row per decision"
    )
    replay.add_argument("--out", default="reports/zone_replay")
    replay.add_argument(
        "--exit-mode",
        choices=("fixed_cash", "next_zone"),
        help="replay only: override holding policy",
    )
    replay.add_argument(
        "--require-structural-stop",
        action="store_true",
        help="replay only: reject cash SL inside setup invalidation",
    )
    from bot.zones.price_action import POLICIES

    study = subparsers.add_parser(
        "candle-test",
        help="compare candle/return/stop/hold policies on actual ordered bid/ask ticks",
    )
    study.add_argument("--config", default="config.yaml")
    study.add_argument("--symbol", default="XAUUSD")
    study.add_argument(
        "--ticks",
        nargs="+",
        required=True,
        help="timestamp,bid,ask UTC CSV/CSV.gz (not candle OHLC)",
    )
    study.add_argument("--policy", choices=(*POLICIES, "all"), default="all")
    study.add_argument("--start")
    study.add_argument("--end")
    study.add_argument(
        "--stop-fill", choices=("barrier", "first_quote"), default="barrier"
    )
    study.add_argument("--out", default="reports/candle_replay_broker")
    export = subparsers.add_parser(
        "export-ticks", help="read-only export from your own MT5 broker terminal"
    )
    export.add_argument("--config", default="config.yaml")
    export.add_argument("--symbol", default="XAUUSD")
    export.add_argument("--start", required=True)
    export.add_argument("--end", required=True, help="exclusive UTC")
    export.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.command == "candle-test":
        from bot.zones.candle_replay import run_price_test

        run_price_test(args, load_yaml(args.config))
        return 0
    if args.command == "export-ticks":
        raw = load_yaml(args.config)
        if "hull" in raw:
            from bot.hull.terminal import export_quotes

            export_quotes(args, raw)
        else:
            from bot.zones.export import run_export

            run_export(args, raw)
        return 0
    if args.command == "zones-replay":
        from bot.zones.replay import run_replay

        run_replay(args, load_yaml(args.config))
        return 0
    if args.command in {"trade", "demo", "trade-all", "hull-preflight", "hull-replay"}:
        from bot.hull.cli import handle

        return handle(args)
    if args.command == "validate-config":
        config = validate_config(load_yaml(Path(args.config)))
        print(json.dumps(config.model_dump(), indent=2, default=str))
        return 0
    if args.command == "features":
        bars = load_ohlcv(args.csv)
        build_multitimeframe_features(bars).to_parquet(args.out)
        return 0
    if args.command == "download":
        bars = fetch_mt5_m1(args.symbol, args.years, allow_partial=args.allow_partial)
        if Path(args.out).suffix.lower() == ".csv":
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            bars.to_csv(args.out, index_label="timestamp")
        else:
            save_ohlcv_parquet(bars, args.out)
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
        model = LogisticBaseline().fit(
            frame[columns].to_numpy(), frame["label"].to_numpy()
        )
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        np.savez(
            output,
            weights=model.weights,
            mean=model.mean_,
            scale=model.scale_,
            columns=columns,
        )
        return 0
    raise RuntimeError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
