from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from bot.config.loader import load_yaml
from bot.config.schema import validate_config
from bot.data.downloader import load_ohlcv_csv
from bot.features.pipeline import build_multitimeframe_features
from bot.labeling.triple_barrier import triple_barrier_labels
from bot.models.baseline import LogisticBaseline


def main() -> int:
    parser = argparse.ArgumentParser(prog="xauusd-bot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-config")
    validate.add_argument("--config", default="config.yaml")
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
    args = parser.parse_args()
    if args.command == "validate-config":
        config = validate_config(load_yaml(Path(args.config)))
        print(json.dumps(config.model_dump(), indent=2, default=str))
        return 0
    if args.command == "features":
        bars = load_ohlcv_csv(args.csv)
        build_multitimeframe_features(bars).to_parquet(args.out)
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
