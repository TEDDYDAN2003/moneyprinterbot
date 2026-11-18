"""Strict offline readers. No data is downloaded or silently fabricated by replay."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .engine import Bar


@dataclass
class Minute:
    bar: Bar
    ms: np.ndarray  # ticks in original order, duplicate millisecond stamps are legal
    bid: np.ndarray  # for OHLC input, [open, high, low, close]; NOT a path
    ask: np.ndarray
    exact: bool
    volume: float = 0.0


def _minute(ms, bid, ask):
    t = int(ms[0] // 60000 * 60)
    return Minute(
        Bar(t, float(bid[0]), float(bid.max()), float(bid.min()), float(bid[-1])),
        ms,
        bid,
        ask,
        True,
        len(ms),
    )


def _check_quotes(bid, ask):
    if not (
        np.isfinite(bid).all()
        and np.isfinite(ask).all()
        and (bid > 0).all()
        and (ask >= bid).all()
    ):
        raise ValueError("Nonfinite, nonpositive, or crossed bid/ask data")


def tick_minutes(paths, override=None):
    """UTC ISO timestamp,bid,ask CSV[.gz]. Bounded memory including file boundaries."""
    carry = None
    for path in paths:
        for df in pd.read_csv(
            path, usecols=["timestamp", "bid", "ask"], chunksize=200_000
        ):
            if df.empty:
                continue
            times = pd.to_datetime(
                df.timestamp, utc=True, format="ISO8601", errors="raise"
            )
            if times.isna().any():
                raise ValueError("Missing tick time")
            ms = pd.DatetimeIndex(times).as_unit("ms").asi8
            bid, ask = df.bid.to_numpy(float), df.ask.to_numpy(float)
            _check_quotes(bid, ask)
            if override is not None:
                ask = bid + override
            if carry is not None:
                ms, bid, ask = (
                    np.concatenate((x, y)) for x, y in zip(carry, (ms, bid, ask))
                )
            if np.any(np.diff(ms) < 0):
                raise ValueError(
                    "Tick timestamps must be ordered across all input files"
                )
            cuts = np.flatnonzero(np.diff(ms // 60000)) + 1
            lo = 0
            for hi in cuts:
                yield _minute(ms[lo:hi], bid[lo:hi], ask[lo:hi])
                lo = hi
            carry = (ms[lo:].copy(), bid[lo:].copy(), ask[lo:].copy())
    if carry is not None:
        yield _minute(*carry)


def bar_minutes(path, spec, source_tz="UTC", override=None):
    """Minute-open BID OHLC. Accept MT5 tab export or normalized CSV/parquet.

    MT5's historical `spread` is a bar statistic, not necessarily the opening
    spread. Lag that value by one bar; never use current-bar future spread to enter.
    Explicit ask_open/high/low/close take precedence when all four exist.
    """
    path = Path(path)
    if path.suffix.lower() in (".pq", ".parquet"):
        df = pd.read_parquet(path)
        if isinstance(df.index, pd.DatetimeIndex):
            df = df.rename_axis("timestamp").reset_index()
    else:
        df = pd.read_csv(path, sep=None, engine="python")
    df.columns = [str(c).strip().strip("<>").lower() for c in df]
    if {"date", "time"}.issubset(df):
        col = df.date.astype(str) + " " + df.time.astype(str)
    elif "timestamp" in df:
        col = df.timestamp
    else:
        raise ValueError(
            "Expected timestamp or MT5 <DATE> and <TIME>; trade logs are not candles"
        )
    times = pd.DatetimeIndex(pd.to_datetime(col, format="mixed", errors="raise"))
    times = (
        times.tz_localize(source_tz, ambiguous="raise", nonexistent="raise")
        if times.tz is None
        else times
    )
    time_ns = times.tz_convert("UTC").as_unit("ns").asi8
    if (
        not len(time_ns)
        or np.any(time_ns % 60_000_000_000)
        or np.any(np.diff(time_ns) <= 0)
    ):
        raise ValueError(
            "M1 open times must be nonempty, unique, ordered and minute-aligned"
        )
    if len(time_ns) > 1 and not np.any(np.diff(time_ns) == 60_000_000_000):
        raise ValueError("No adjacent 1-minute observations; supply M1, not M5/M15")
    cols = ["open", "high", "low", "close"]
    if not set(cols) <= set(df):
        raise ValueError("Missing bid OHLC price columns")
    bids = df[cols].to_numpy(float)
    ask_cols = ["ask_" + c for c in cols]
    if any(c in df for c in ask_cols) and not all(c in df for c in ask_cols):
        raise ValueError("Partial ask OHLC")
    if override is not None:
        asks = bids + override
    elif all(c in df for c in ask_cols):
        asks = df[ask_cols].to_numpy(float)
    else:
        spreads = np.full(len(df), spec.assumed_spread)
        if "spread" in df:
            observed = df.spread.to_numpy(float) * spec.point
            if not np.isfinite(observed).all() or (observed < 0).any():
                raise ValueError("Invalid MT5 spread points")
            spreads[1:] = observed[:-1]
        asks = bids + spreads[:, None]
    _check_quotes(bids, asks)
    vcol = next(
        (k for k in ("tick_volume", "tickvol", "volume", "vol") if k in df), None
    )
    volume = df[vcol].to_numpy(float) if vcol else np.zeros(len(df))
    if not np.isfinite(volume).all() or (volume < 0).any():
        raise ValueError("Invalid volume")
    for ns, bid, ask, vol in zip(time_ns, bids, asks, volume):
        t = int(ns // 1_000_000_000)
        Bar(t, *ask)  # validate independent ask extremes
        yield Minute(
            Bar(t, *bid),
            np.array([t * 1000] * 3 + [(t + 60) * 1000 - 1]),
            bid,
            ask,
            False,
            float(vol),
        )
