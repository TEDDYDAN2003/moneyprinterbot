"""Reproduce the fixed research snapshot from public Exness ZIPs (no MT5 or journal inputs).

Run from anywhere: python research/session_volatility/study.py /path/to/monthly-zips
See README.md for the input manifest, the HTTPS download command and methodology.
"""

import argparse
import csv
import hashlib
import json
import shutil
import sys
import zipfile
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from urllib.request import urlopen
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "HullJournal"))
from hull_analytics import session_minute

MONTHS = ("2026_09", "2026_10")
# One row per quoted market. No separate DE40/DE30 series. The last two are chat
# research comparators only, never injected into either the dashboard or the bot.
MARKETS = {
    "JP225m": (("JP225",), "Nikkei 225 · Japan", "Indices"),
    "DE30m": (("DE40", "DE30"), "DAX · Germany", "Indices"),
    "US500m": (("US500",), "S&P 500 · US", "Indices"),
    "USTEC_x100m": (("NAS100USD",), "Nasdaq-100 · US", "Indices"),
    "US30m": (("US30",), "Dow Jones · US", "Indices"),
    "XAUUSDm": (("XAUUSD",), "Gold · global / USD", "Metals"),
    "GBPJPYm": (("GBPJPY",), "Sterling / yen · UK & Japan", "FX"),
    "EURUSDm": (("EURUSD",), "Euro / dollar · euro area & US", "FX"),
    "GBPUSDm": (("GBPUSD",), "Sterling / dollar · UK & US", "FX"),
    "USDJPYm": (("USDJPY",), "Dollar / yen · US & Japan", "FX"),
    "AUDUSDm": (("AUDUSD",), "Aussie / dollar · Australia & US", "FX"),
    "HK50m": ((), "Hang Seng · Hong Kong", "Comparison only"),
    "XAGUSDm": ((), "Silver · global / USD", "Comparison only"),
}
BUCKETS = ("Asia-Pacific", "London", "London + New York", "New York", "Off-session")
OPENINGS = (
    ("Tokyo open", "Asia/Tokyo", 9 * 60),
    ("Frankfurt open", "Europe/Berlin", 9 * 60),
    ("New York open", "America/New_York", 9 * 60 + 30),
)


def broad(exact):
    active = set(exact.split(" + "))
    if {"London", "New York"} <= active:
        return BUCKETS[2]
    if "London" in active:
        return BUCKETS[1]
    if "New York" in active:
        return BUCKETS[3]
    if active & {"Tokyo", "Sydney"}:
        return BUCKETS[0]
    if exact != "Off-session":
        raise ValueError(f"Invalid session: {exact}; install tzdata")
    return BUCKETS[4]


@lru_cache(maxsize=100_000)
def clock_windows(minute):
    dt = datetime.fromtimestamp(minute * 60, UTC)
    tags = [broad(session_minute(minute))]
    for name, zone, begin in OPENINGS:
        local = dt.astimezone(ZoneInfo(zone))
        if local.weekday() < 5 and begin <= local.hour * 60 + local.minute < begin + 60:
            tags.append(name)
    return tags


def quote_bars(path, symbol, month, chunksize=150_000):
    """Pool fragments across chunk boundaries. A 5m bar needs all five quoted minutes."""
    lo = pd.Timestamp(month.replace("_", "-") + "-01", tz="UTC")
    end = (lo + pd.offsets.MonthBegin(1)).timestamp() * 1000
    parts = []
    ticks = bad = 0
    prev_ms = -1
    with zipfile.ZipFile(path) as z:
        if z.namelist() != [path.stem + ".csv"]:
            raise ValueError(f"Unexpected archive member: {path}")
        with z.open(z.namelist()[0]) as f:
            for df in pd.read_csv(
                f,
                chunksize=chunksize,
                dtype={"Exness": "category", "Symbol": "category"},
            ):
                if list(df.columns) != [
                    "Exness",
                    "Symbol",
                    "Timestamp",
                    "Bid",
                    "Ask",
                ] or not (df.Exness.eq("exness").all() and df.Symbol.eq(symbol).all()):
                    raise ValueError(f"Unexpected feed: {path}")
                ms = (
                    pd.DatetimeIndex(
                        pd.to_datetime(df.Timestamp, format="ISO8601", utc=True)
                    )
                    .as_unit("ms")
                    .asi8
                )
                if (
                    ms.min() < lo.timestamp() * 1000
                    or ms.max() >= end
                    or ms[0] < prev_ms
                    or np.any(np.diff(ms) < 0)
                ):
                    raise ValueError(f"Out of range or unordered timestamps: {path}")
                prev_ms = ms[-1]
                bid, ask = df.Bid.to_numpy(), df.Ask.to_numpy()
                ok = np.isfinite(bid) & np.isfinite(ask) & (bid > 0) & (ask >= bid)
                ticks += len(ms)
                bad += int((~ok).sum())
                mid = (bid[ok] + ask[ok]) / 2
                frame = pd.DataFrame(
                    {"mid": mid, "spr": (ask[ok] - bid[ok]) / mid * 10_000},
                    index=ms[ok] // 60_000,
                )
                parts.append(
                    frame.groupby(level=0).agg(
                        op=("mid", "first"),
                        hi=("mid", "max"),
                        lo=("mid", "min"),
                        spr=("spr", "first"),
                    )
                )
    mins = (
        pd.concat(parts)
        .groupby(level=0)
        .agg({"op": "first", "hi": "max", "lo": "min", "spr": "first"})
    )
    bars = mins.groupby(mins.index // 5 * 5).agg(
        op=("op", "first"),
        hi=("hi", "max"),
        lo=("lo", "min"),
        spr=("spr", "median"),
        coverage=("op", "count"),
    )
    partial = int((bars.coverage < 5).sum())
    bars = bars[bars.coverage == 5].copy()
    bars["range"] = (bars.hi - bars.lo) / bars.op * 10_000
    return bars[["range", "spr"]], {
        "ticks": ticks,
        "invalid_quotes": bad,
        "partial_nonempty_bars": partial,
        "complete_bars": len(bars),
    }


def tagged_bars(bars):
    frame = bars.copy()
    frame["day"] = frame.index // 1440
    frame["tag"] = [clock_windows(int(m)) for m in frame.index]
    return frame.explode("tag")


def summarise(frame):
    out = {}
    for tag, g in frame.groupby("tag"):
        out[tag] = {
            "n": len(g),
            "days": int(g.day.nunique()),
            "range": round(float(g.range.median()), 4),
            "spread": round(float(g.spr.median()), 4),
        }
    return out


def csv_output(name, rows):
    with (HERE / name).open("w", newline="", encoding="utf8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def write_json(p, obj):
    p.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf8",
    )


def pinned_archive(raw, sym, month, pin, download):
    name = f"Exness_{sym}_{month}.zip"
    url = f"https://ticks.ex2archive.com/ticks/{sym}/{month.replace('_', '/')}/{name}"
    if pin["name"] != name or pin["url"] != url:
        raise ValueError(f"Unexpected manifest entry: {name}")
    path = raw / name
    part = path.with_suffix(".part")
    fetched = not path.exists() and download
    try:
        if fetched:
            raw.mkdir(parents=True, exist_ok=True)
            with urlopen(url, timeout=60) as response, part.open("wb") as out:
                shutil.copyfileobj(response, out, 1024 * 1024)
        with (part if fetched else path).open("rb") as f:
            if hashlib.file_digest(f, "sha256").hexdigest() != pin["sha256"]:
                raise ValueError(f"Archive hash differs from pinned input: {name}")
        if fetched:
            part.replace(path)
    finally:
        if fetched:
            part.unlink(missing_ok=True)
    return path


def main(raw, download=False):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())["hull"]["symbols"]
    journal = {"start": "2026-09-01", "end": "2026-10-31", "markets": []}
    pins = {r["name"]: r for r in json.loads((HERE / "archives.json").read_text())}
    csv_rows, day_rows = [], []
    for sym, (keys, base, kind) in MARKETS.items():
        chunks = []
        for month in MONTHS:
            name = f"Exness_{sym}_{month}.zip"
            pin = pins[name]
            path = pinned_archive(raw, sym, month, pin, download)
            bars, audit = quote_bars(path, sym, month)
            if any(pin[k] != v for k, v in audit.items()):
                raise ValueError(f"Archive counts differ: {name}")
            chunks.append(bars)
        allbars = pd.concat(chunks, verify_integrity=True)
        tagged = tagged_bars(allbars)
        stats = summarise(tagged)
        for (d, tag), g in tagged.groupby(["day", "tag"]):
            day_rows.append(
                {
                    "broker_symbol": sym,
                    "utc_date": datetime.fromtimestamp(int(d) * 86400, UTC)
                    .date()
                    .isoformat(),
                    "window": tag,
                    "m5_bars": len(g),
                    "median_range_bp": round(float(g.range.median()), 4),
                    "median_spread_bp": round(float(g.spr.median()), 4),
                }
            )
        for tag in (*BUCKETS, *(a[0] for a in OPENINGS)):
            s = stats.get(tag)
            if s:
                csv_rows.append(
                    {
                        "broker_symbol": sym,
                        "profiles": " / ".join(keys),
                        "window": tag,
                        "m5_bars": s["n"],
                        "utc_dates": s["days"],
                        "median_range_bp": s["range"],
                        "median_spread_bp": s["spread"],
                    }
                )
        if keys:
            aliases = sorted(
                {sym, *keys, *(a for k in keys for a in cfg[k]["aliases"])}
            )
            if any(
                cfg[k].get("broker_symbol", sym) != sym for k in keys
            ) or sym not in {
                a
                for k in keys
                for a in [*cfg[k]["aliases"], cfg[k].get("broker_symbol")]
            }:
                raise ValueError(f"Profile mismatch: {sym}")
            journal["markets"].append(
                {
                    "key": " / ".join(keys),
                    "symbol": sym,
                    "base": base,
                    "kind": kind,
                    "aliases": aliases,
                    "stats": stats,
                }
            )
        print(
            sym, len(allbars), {tag: a["range"] for tag, a in stats.items()}, flush=True
        )
    csv_output("windows.csv", csv_rows)
    csv_output("windows_by_utc_date.csv", day_rows)
    write_json(ROOT / "HullJournal/ui/market_windows.json", journal)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("archive_dir", type=Path)
    ap.add_argument(
        "--download",
        action="store_true",
        help="fetch missing pinned archives before measuring; no broker login",
    )
    args = ap.parse_args()
    main(args.archive_dir, args.download)
