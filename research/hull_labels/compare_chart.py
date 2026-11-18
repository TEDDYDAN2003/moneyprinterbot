"""Compare against YOUR actual chart's OHLC and labels, without MT5 or order permission."""

import argparse
import csv
from pathlib import Path

import pandas as pd

from bot.config.loader import load_yaml
from bot.hull.config import HullConfig
from bot.hull.signal import Hull
from bot.zones.engine import Bar


def compare(path, config, key, tf, out, last_live):
    h = HullConfig.model_validate(load_yaml(config)["hull"])
    eng = Hull(tf, h.indicator, h.symbols[key].pip_size)
    df = pd.read_csv(path, keep_default_na=False)
    required = {"timestamp", "open", "high", "low", "close"}
    if not required <= set(df):
        raise ValueError("Provide timestamp (UTC bar open),open,high,low,close[,label]")
    if last_live:
        df = df.iloc[:-1]
    has = "label" in df
    bad = total = 0
    out.parent.mkdir(exist_ok=True, parents=True)
    with out.open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(
            [
                "timestamp",
                "a",
                "b",
                "label",
                "chart_label",
                "anchor_s",
                "anchor",
                "raw_sl",
            ]
        )
        for r in df.itertuples(index=False):
            t = pd.Timestamp(r.timestamp)
            t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
            if t.value % 60_000_000_000:
                raise ValueError("Use minute-open UTC timestamps")
            d = eng.update(Bar(int(t.timestamp()), r.open, r.high, r.low, r.close))
            v = {1: "BUY", -1: "SELL", 0: ""}[d.label]
            expected = str(r.label).strip().upper() if has else ""
            if expected not in ("", "BUY", "SELL"):
                raise ValueError("Optional label must be BUY, SELL or blank")
            bad += int(has and v != expected)
            total += bool(v)
            w.writerow(
                [t.isoformat(), d.a, d.b, v, expected, d.anchor_s, d.anchor, d.raw_sl]
            )
    print(
        f"{len(df)} closed candles, {total} raw labels; "
        + (f"{bad} label mismatches" if has else "no expected labels supplied")
    )
    return int(bool(bad))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", type=int, choices=[1, 2, 5], required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument(
        "--last-live",
        action="store_true",
        help="Exclude the last row when it is the still-forming candle",
    )
    a = ap.parse_args()
    raise SystemExit(compare(a.bars, a.config, a.symbol, a.tf, a.out, a.last_live))
