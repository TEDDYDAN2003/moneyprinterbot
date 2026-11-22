"""Export the user's broker quotes, read-only. Never logs into an account or sends orders."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd


def file_hash(path):
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def export(mt, name, start, end, out):
    """Bounded hourly queries; MT5 endpoints may be inclusive. Keep valid same-ms rows."""
    lo, hi = [pd.to_datetime(v, utc=True, errors="raise") for v in (start, end)]
    if (
        pd.isna(lo)
        or pd.isna(hi)
        or lo >= hi
        or any(v.value % 1_000_000_000 for v in (lo, hi))
    ):
        raise ValueError("Use ordered, whole-second UTC bounds; end is exclusive")
    out = Path(out)
    meta = out.with_name(out.name + ".manifest.json")
    if out.exists() or meta.exists():
        raise FileExistsError("Choose a new export name; files are not overwritten")
    if not (out.name.endswith(".csv") or out.name.endswith(".csv.gz")):
        raise ValueError("Use .csv or .csv.gz")
    if not mt.symbol_select(name, True):
        raise ValueError(f"Broker symbol not available: {name}")
    info = mt.symbol_info(name)
    if info is None:
        raise ValueError("Missing broker specification")
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=out.name + ".", suffix=".partial", dir=out.parent)
    os.close(fd)
    first = last = None
    total = 0
    empty = []
    a = int(lo.timestamp())
    stop = int(hi.timestamp())
    try:
        opener = gzip.open if out.name.endswith(".gz") else open
        with opener(tmp, "wt", encoding="utf-8", newline="") as f:
            f.write("timestamp,bid,ask\n")
            while a < stop:
                b = min(a + 3600, stop)
                raw = mt.copy_ticks_range(
                    name,
                    datetime.fromtimestamp(a, UTC),
                    datetime.fromtimestamp(b, UTC),
                    mt.COPY_TICKS_ALL,
                )
                if raw is None:
                    raise RuntimeError(
                        f"MT5 tick query failed starting at {a}: {mt.last_error()}"
                    )
                if len(raw):
                    df = pd.DataFrame(raw)
                    df = df.loc[
                        (df.time_msc >= a * 1000) & (df.time_msc < b * 1000),
                        ["time_msc", "bid", "ask"],
                    ]
                else:
                    df = pd.DataFrame()
                if df.empty:
                    empty.append(a)
                else:
                    m = df.time_msc.to_numpy("int64")
                    bids = df.bid.to_numpy(float)
                    asks = df.ask.to_numpy(float)
                    if (
                        (np.diff(m) < 0).any()
                        or (last is not None and m[0] < last)
                        or not (
                            np.isfinite(bids).all()
                            and np.isfinite(asks).all()
                            and (bids > 0).all()
                            and (asks >= bids).all()
                        )
                    ):
                        raise ValueError(
                            "Malformed or unordered broker quotes; no file published"
                        )
                    if first is None:
                        first = int(m[0])
                    last = int(m[-1])
                    total += len(m)
                    df.insert(
                        0,
                        "timestamp",
                        pd.to_datetime(m, unit="ms", utc=True).strftime(
                            "%Y-%m-%dT%H:%M:%S.%fZ"
                        ),
                    )
                    df.to_csv(
                        f,
                        columns=["timestamp", "bid", "ask"],
                        header=False,
                        index=False,
                        lineterminator="\n",
                    )
                a = b
        if not total:
            raise ValueError(
                "No historical ticks returned. Check symbol, dates and terminal history."
            )
        receipt = {
            "broker_symbol": name,
            "utc_start": str(lo),
            "utc_end_exclusive": str(hi),
            "first_ms": first,
            "last_ms": last,
            "ticks": total,
            "empty_hour_starts_epoch_s": empty,
            "point": info.point,
            "tick_size": info.trade_tick_size,
            "contract_size": info.trade_contract_size,
            "profit_currency": info.currency_profit,
            "sha256": file_hash(tmp),
        }
        os.replace(tmp, out)
        meta.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        return receipt
    finally:
        Path(tmp).unlink(missing_ok=True)
