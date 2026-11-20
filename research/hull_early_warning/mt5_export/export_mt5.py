"""Read-only, resumable export of native US30m M1 bars and MT5 ticks."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import pyarrow  # noqa: F401
except ImportError:
    print("Missing pyarrow. Install it in the repository .venv before exporting.", file=sys.stderr)
    raise SystemExit(2)

import MetaTrader5 as mt5


UTC = timezone.utc
SYMBOL = "US30m"
ROOT = Path(__file__).resolve().parent
BULK = ROOT / "bulk"
SAMPLE = ROOT / "sample"
MANIFEST_PATH = ROOT / "manifest.json"
METADATA_PATH = ROOT / "symbol_metadata.json"
COVERAGE_PATH = ROOT / "coverage_report.json"
START = datetime(2023, 12, 1, tzinfo=UTC)
EVAL_START = datetime(2024, 1, 1, tzinfo=UTC)
END = datetime(2026, 1, 1, tzinfo=UTC)
PILOT_DATE = date(2024, 1, 3)  # Wednesday, ordinary training-period trading day.
SPLIT_ROWS = 250_000
MIN_SPLIT_SECONDS = 60
RESERVE_BYTES = 2 * 1024**3


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def split_name(value: datetime) -> str:
    if value < EVAL_START:
        return "warmup_2023_12"
    if value < datetime(2025, 1, 1, tzinfo=UTC):
        return "training_2024"
    if value < datetime(2025, 7, 1, tzinfo=UTC):
        return "validation_2025_H1"
    return "final_test_2025_H2"


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        try:
            value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            if value.get("schema_version") == 1:
                return value
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "schema_version": 1,
        "symbol": SYMBOL,
        "requested_bounds_utc": {"start_inclusive": iso(START), "end_exclusive": iso(END)},
        "partitions": [],
        "samples": [],
        "empty_chunks": [],
        "failed_chunks": [],
        "diagnostic_probes": [],
        "pilot": {},
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verified(record: dict) -> bool:
    path = ROOT / record["relative_path"]
    return (
        path.is_file()
        and path.stat().st_size == record.get("bytes")
        and sha256(path) == record.get("sha256")
    )


def coverage_of(manifest: dict, dataset: str, start: datetime, end: datetime) -> bool:
    records = []
    for item in manifest["partitions"]:
        if item.get("dataset") != dataset:
            continue
        left, right = parse_iso(item["start_utc"]), parse_iso(item["end_utc"])
        if right > start and left < end and verified(item):
            records.append((left, right))
    cursor = start
    for left, right in sorted(records):
        if left > cursor:
            return False
        if right > cursor:
            cursor = right
        if cursor >= end:
            return True
    return cursor >= end


def observed_coverage(manifest: dict, dataset: str, start: datetime, end: datetime) -> bool:
    """True only when saved rows or explicit empty responses tile the full interval."""
    records = []
    for item in manifest["partitions"]:
        if item.get("dataset") == dataset and verified(item):
            records.append((parse_iso(item["start_utc"]), parse_iso(item["end_utc"])))
    for item in manifest["empty_chunks"]:
        if item.get("dataset") == dataset and item.get("start_utc") and item.get("end_utc"):
            records.append((parse_iso(item["start_utc"]), parse_iso(item["end_utc"])))
    cursor = start
    for left, right in sorted(records):
        if right <= cursor or left >= end:
            continue
        if left > cursor:
            return False
        cursor = max(cursor, min(right, end))
        if cursor >= end:
            return True
    return cursor >= end


def record_for(manifest: dict, dataset: str, start: datetime, end: datetime) -> dict | None:
    for item in manifest["partitions"]:
        if (
            item.get("dataset") == dataset
            and item.get("start_utc") == iso(start)
            and item.get("end_utc") == iso(end)
            and verified(item)
        ):
            return item
    return None


def parquet_record(dataset: str, start: datetime, end: datetime, frame: pd.DataFrame,
                   output: Path, manifest: dict) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    frame.to_parquet(tmp, index=False, compression="zstd")
    if len(pd.read_parquet(tmp)) != len(frame):
        tmp.unlink(missing_ok=True)
        raise IOError(f"Parquet row-count verification failed: {output}")
    os.replace(tmp, output)
    item = {
        "dataset": dataset,
        "relative_path": output.relative_to(ROOT).as_posix(),
        "start_utc": iso(start),
        "end_utc": iso(end),
        "row_count": int(len(frame)),
        "first_utc": frame["time_utc"].iloc[0].isoformat() if len(frame) else None,
        "last_utc": frame["time_utc"].iloc[-1].isoformat() if len(frame) else None,
        "bytes": output.stat().st_size,
        "sha256": sha256(output),
    }
    manifest["partitions"] = [
        x for x in manifest["partitions"]
        if not (x.get("dataset") == dataset and x.get("start_utc") == item["start_utc"]
                and x.get("end_utc") == item["end_utc"])
    ]
    manifest["partitions"].append(item)
    atomic_json(MANIFEST_PATH, manifest)
    return item


def filtered_frame(raw, dataset: str, start: datetime, end: datetime) -> pd.DataFrame:
    if raw is None:
        raise ValueError("MT5 returned None")
    frame = pd.DataFrame.from_records(raw)
    if dataset == "bars":
        seconds = frame["time"].astype("int64")
        frame = frame.loc[(seconds >= int(start.timestamp())) & (seconds < int(end.timestamp()))].copy()
        frame["time"] = frame["time"].astype("int64")
        frame["time_utc"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    else:
        millis = frame["time_msc"].astype("int64")
        frame = frame.loc[
            (millis >= int(start.timestamp() * 1000))
            & (millis < int(end.timestamp() * 1000))
        ].copy()
        frame["time_msc"] = frame["time_msc"].astype("int64")
        frame["time_utc"] = pd.to_datetime(frame["time_msc"], unit="ms", utc=True)
    frame["research_split"] = frame["time_utc"].map(
        lambda value: split_name(value.to_pydatetime())
    )
    return frame.reset_index(drop=True)


def fetch(mt5, dataset: str, start: datetime, end: datetime) -> pd.DataFrame | None:
    if dataset == "bars":
        raw = mt5.copy_rates_range(SYMBOL, mt5.TIMEFRAME_M1, start, end)
    else:
        raw = mt5.copy_ticks_range(SYMBOL, start, end, mt5.COPY_TICKS_ALL)
    if raw is None:
        return None
    return filtered_frame(raw, dataset, start, end)


def add_empty(manifest: dict, dataset: str, start: datetime, end: datetime,
              reason: str = "MT5 returned zero rows") -> None:
    item = {"dataset": dataset, "start_utc": iso(start), "end_utc": iso(end), "reason": reason}
    if item not in manifest["empty_chunks"]:
        manifest["empty_chunks"].append(item)
        atomic_json(MANIFEST_PATH, manifest)


def add_failure(manifest: dict, dataset: str, start: datetime, end: datetime, error: object) -> None:
    item = {
        "dataset": dataset, "start_utc": iso(start), "end_utc": iso(end),
        "error": str(error), "mt5_last_error": repr(mt5.last_error()),
    }
    if item not in manifest["failed_chunks"]:
        manifest["failed_chunks"].append(item)
        atomic_json(MANIFEST_PATH, manifest)


def clear_chunk_status(manifest: dict, dataset: str, start: datetime, end: datetime) -> None:
    left, right = iso(start), iso(end)
    manifest["empty_chunks"] = [
        x for x in manifest["empty_chunks"]
        if not (x.get("dataset") == dataset and x.get("start_utc") == left
                and x.get("end_utc") == right)
    ]
    manifest["failed_chunks"] = [
        x for x in manifest["failed_chunks"]
        if not (x.get("dataset") == dataset and x.get("start_utc") == left
                and x.get("end_utc") == right)
    ]


def tick_chunk(mt5, start: datetime, end: datetime, manifest: dict,
               split_rows: int = SPLIT_ROWS) -> None:
    if coverage_of(manifest, "ticks", start, end):
        return
    frame = fetch(mt5, "ticks", start, end)
    if frame is None:
        add_failure(manifest, "ticks", start, end, "copy_ticks_range returned None")
        return
    if frame.empty:
        add_empty(manifest, "ticks", start, end)
        return
    clear_chunk_status(manifest, "ticks", start, end)
    seconds = int((end - start).total_seconds())
    if len(frame) >= split_rows and seconds > MIN_SPLIT_SECONDS:
        middle = start + timedelta(seconds=seconds // 2)
        tick_chunk(mt5, start, middle, manifest, split_rows)
        tick_chunk(mt5, middle, end, manifest, split_rows)
        return
    stamp = f"{start:%Y%m%dT%H%M%SZ}_{end:%Y%m%dT%H%M%SZ}"
    parquet_record("ticks", start, end, frame, BULK / "ticks" / f"{stamp}.parquet", manifest)


def month_windows(start: datetime, end: datetime):
    cursor = datetime(start.year, start.month, 1, tzinfo=UTC)
    while cursor < end:
        if cursor.month == 12:
            nxt = datetime(cursor.year + 1, 1, 1, tzinfo=UTC)
        else:
            nxt = datetime(cursor.year, cursor.month + 1, 1, tzinfo=UTC)
        yield cursor, min(nxt, end)
        cursor = nxt


def day_windows(start: datetime, end: datetime):
    cursor = start
    while cursor < end:
        nxt = min(cursor + timedelta(days=1), end)
        yield cursor, nxt
        cursor = nxt


def count_weekdays(start: datetime, end: datetime) -> int:
    current, count = start.date(), 0
    while current < end.date():
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def write_sample(bars: pd.DataFrame, ticks: pd.DataFrame, manifest: dict) -> list[int]:
    if bars.empty or ticks.empty:
        return []
    minute = (ticks["time_msc"].to_numpy(dtype=np.int64) // 60_000)
    minutes = np.unique(minute)[:5]
    keep_ticks = ticks.loc[np.isin(minute, minutes)].copy()
    keep_bars = bars.loc[
        np.isin(bars["time"].to_numpy(dtype=np.int64) // 60, minutes)
    ].copy()
    SAMPLE.mkdir(parents=True, exist_ok=True)
    for name, frame in (("training_day_bars.parquet", keep_bars),
                        ("training_day_ticks.parquet", keep_ticks)):
        path = SAMPLE / name
        tmp = path.with_name(path.name + ".tmp")
        frame.to_parquet(tmp, index=False, compression="zstd")
        os.replace(tmp, path)
        manifest["samples"] = [x for x in manifest["samples"] if x.get("relative_path") != path.relative_to(ROOT).as_posix()]
        manifest["samples"].append({
            "relative_path": path.relative_to(ROOT).as_posix(),
            "row_count": int(len(frame)),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
            "note": "All MT5 rows from the first five tick-bearing UTC minutes of the pilot day; sample only, excluded from canonical partitions.",
        })
    atomic_json(MANIFEST_PATH, manifest)
    return [int(x) for x in minutes]


def weekend_gap(previous_s: int, current_s: int) -> bool:
    day = datetime.fromtimestamp(previous_s, UTC).date()
    last = datetime.fromtimestamp(current_s, UTC).date()
    while day <= last:
        if day.weekday() >= 5:
            return True
        day += timedelta(days=1)
    return False


def audit_month(month: datetime, manifest: dict, tick_size: float) -> dict:
    month_end = next(iter([b for a, b in month_windows(month, END)]))
    bar_items = [
        x for x in manifest["partitions"]
        if x.get("dataset") == "bars" and parse_iso(x["start_utc"]) < month_end
        and parse_iso(x["end_utc"]) > month and verified(x)
    ]
    tick_items = [
        x for x in manifest["partitions"]
        if x.get("dataset") == "ticks" and parse_iso(x["start_utc"]) < month_end
        and parse_iso(x["end_utc"]) > month and verified(x)
    ]
    bar_complete = observed_coverage(manifest, "bars", month, month_end)
    tick_complete = observed_coverage(manifest, "ticks", month, month_end)
    bar_attempted = any(
        x.get("dataset") == "bars"
        and parse_iso(x["start_utc"]) < month_end
        and parse_iso(x["end_utc"]) > month
        for x in manifest["empty_chunks"] + manifest["failed_chunks"]
        if x.get("start_utc") and x.get("end_utc")
    )
    tick_attempted = any(
        x.get("dataset") == "ticks"
        and parse_iso(x["start_utc"]) < month_end
        and parse_iso(x["end_utc"]) > month
        for x in manifest["empty_chunks"] + manifest["failed_chunks"]
        if x.get("start_utc") and x.get("end_utc")
    )
    bars = pd.concat([pd.read_parquet(ROOT / x["relative_path"]) for x in bar_items],
                     ignore_index=True) if bar_items else pd.DataFrame()
    bars = bars.loc[(bars["time_utc"] >= month) & (bars["time_utc"] < month_end)] if not bars.empty else bars
    ticks_by_minute: dict[int, dict] = {}
    tick_rows = invalid_quotes = crossed_quotes = out_of_order = 0
    tick_first = tick_last = None
    prev_last_msc = None
    long_tick_gaps = []
    for item in sorted(tick_items, key=lambda x: x["start_utc"]):
        frame = pd.read_parquet(ROOT / item["relative_path"])
        if frame.empty:
            continue
        ms = frame["time_msc"].to_numpy(dtype=np.int64)
        bid = frame["bid"].to_numpy(dtype=np.float64)
        ask = frame["ask"].to_numpy(dtype=np.float64)
        tick_rows += len(frame)
        tick_first = tick_first or frame["time_utc"].iloc[0].isoformat()
        tick_last = frame["time_utc"].iloc[-1].isoformat()
        out_of_order += int(np.count_nonzero(np.diff(ms) < 0))
        if prev_last_msc is not None and len(ms) and ms[0] < prev_last_msc:
            out_of_order += 1
        prev_last_msc = int(ms[-1])
        finite = np.isfinite(bid) & np.isfinite(ask)
        positive = finite & (bid > 0) & (ask > 0)
        crossed = positive & (bid > ask)
        valid_quote = positive & (bid <= ask)
        invalid_quotes += int(np.count_nonzero(~positive))
        crossed_quotes += int(np.count_nonzero(crossed))
        minute_key = (ms // 60_000) * 60
        for key, indexes in pd.Series(np.arange(len(ms))).groupby(minute_key, sort=False):
            ix = indexes.to_numpy(dtype=np.int64)
            vq = int(np.count_nonzero(valid_quote[ix]))
            vb = ix[np.isfinite(bid[ix]) & (bid[ix] > 0)]
            if not len(vb):
                stats = {"rows": len(ix), "usable_quotes": vq, "bid_ohlc": None}
            else:
                values = bid[vb]
                stats = {
                    "rows": len(ix), "usable_quotes": vq,
                    "bid_ohlc": [float(values[0]), float(np.max(values)),
                                 float(np.min(values)), float(values[-1])],
                }
            k = int(key)
            prior = ticks_by_minute.get(k)
            if prior is None:
                ticks_by_minute[k] = stats
            else:
                prior["rows"] += stats["rows"]
                prior["usable_quotes"] += stats["usable_quotes"]
                if stats["bid_ohlc"]:
                    if prior["bid_ohlc"] is None:
                        prior["bid_ohlc"] = stats["bid_ohlc"]
                    else:
                        prior["bid_ohlc"][1] = max(prior["bid_ohlc"][1], stats["bid_ohlc"][1])
                        prior["bid_ohlc"][2] = min(prior["bid_ohlc"][2], stats["bid_ohlc"][2])
                        prior["bid_ohlc"][3] = stats["bid_ohlc"][3]
        if len(ms) > 1:
            for i in np.flatnonzero(np.diff(ms) > 300_000)[:5]:
                a, b = int(ms[i] // 1000), int(ms[i + 1] // 1000)
                long_tick_gaps.append({
                    "from_utc": datetime.fromtimestamp(a, UTC).isoformat(),
                    "to_utc": datetime.fromtimestamp(b, UTC).isoformat(),
                    "seconds": b - a,
                    "classification": "weekend_closure_candidate" if weekend_gap(a, b)
                    else "unclassified_non_weekend_gap",
                })
    no_usable = compared = matched = 0
    mismatches = []
    bar_gaps = []
    if not bars.empty:
        times = bars["time"].to_numpy(dtype=np.int64)
        for i in range(1, len(times)):
            delta = int(times[i] - times[i - 1])
            if delta > 60:
                prev_s, cur_s = int(times[i - 1]), int(times[i])
                bar_gaps.append({
                    "from_utc": datetime.fromtimestamp(prev_s, UTC).isoformat(),
                    "to_utc": datetime.fromtimestamp(cur_s, UTC).isoformat(),
                    "missing_minutes": max(0, delta // 60 - 1),
                    "classification": "weekend_closure_candidate" if weekend_gap(prev_s, cur_s)
                    else "unclassified_non_weekend_gap",
                })
        for row in bars.itertuples(index=False):
            key = (int(row.time) // 60) * 60
            stats = ticks_by_minute.get(key)
            if not stats or stats["usable_quotes"] == 0:
                no_usable += 1
            if not stats or stats["bid_ohlc"] is None:
                continue
            compared += 1
            native = np.array([row.open, row.high, row.low, row.close], dtype=float)
            reconstructed = np.array(stats["bid_ohlc"], dtype=float)
            diffs = np.abs(native - reconstructed)
            if np.all(diffs <= tick_size + 1e-12):
                matched += 1
            elif len(mismatches) < 5:
                mismatches.append({
                    "bar_open_utc": datetime.fromtimestamp(int(row.time), UTC).isoformat(),
                    "native_ohlc": native.tolist(),
                    "tick_bid_ohlc": reconstructed.tolist(),
                    "absolute_differences": diffs.tolist(),
                })
    return {
        "month_utc": month.strftime("%Y-%m"),
        "status": "complete" if bar_complete and tick_complete else (
            "partial" if bar_items or tick_items or bar_attempted or tick_attempted
            else "not_exported"
        ),
        "m1_bars": int(len(bars)) if bar_complete else None,
        "ticks": tick_rows if tick_complete else None,
        "first_bar_utc": None if bars.empty else bars["time_utc"].iloc[0].isoformat(),
        "last_bar_utc": None if bars.empty else bars["time_utc"].iloc[-1].isoformat(),
        "first_tick_utc": tick_first,
        "last_tick_utc": tick_last,
        "bars_without_usable_bid_ask_ticks": no_usable if tick_complete else None,
        "invalid_quotes_bid_or_ask_nonpositive_or_nonfinite": invalid_quotes if tick_complete else None,
        "crossed_quotes": crossed_quotes if tick_complete else None,
        "out_of_order_tick_timestamps": out_of_order if tick_complete else None,
        "bid_ohlc_compared_bars": compared if tick_complete else None,
        "bid_ohlc_matches_within_one_trade_tick": matched if tick_complete else None,
        "bid_ohlc_match_pct": None if compared == 0 else 100.0 * matched / compared,
        "bid_ohlc_mismatches_sample": mismatches if tick_complete else None,
        "bar_gaps": bar_gaps,
        "tick_gaps_over_5m_sample": long_tick_gaps[:10],
        "gap_classification_note": "Weekend-crossing gaps are closure candidates. Other gaps are unclassified because the broker session calendar is not assumed.",
    }


def write_coverage(manifest: dict, metadata: dict, observation: dict | None = None) -> None:
    months = [audit_month(m, manifest, float(metadata["observed_specifications"]["trade_tick_size"] or metadata["observed_specifications"]["point"]))
              for m, _ in month_windows(START, END)]
    bars_known = sum(x.get("m1_bars") or 0 for x in months)
    ticks_known = sum(x.get("ticks") or 0 for x in months)
    complete_months = bool(months) and all(x["status"] == "complete" for x in months)
    if complete_months and bars_known and ticks_known:
        verdict = "READY for intrabar research"
    elif bars_known and not ticks_known:
        verdict = "PARTIAL with exact limitations"
    else:
        verdict = "PARTIAL with exact limitations"
    report = {
        "symbol": SYMBOL,
        "status": verdict,
        "requested_bounds_utc": manifest["requested_bounds_utc"],
        "warmup": "December 2023, reported separately and excluded from evaluation.",
        "splits": {
            "training": "2024-01-01 through 2024-12-31",
            "validation": "2025-01-01 through 2025-06-30",
            "untouched_final_test": "2025-07-01 through 2025-12-31",
        },
        "pilot": manifest.get("pilot", {}),
        "empty_chunks": manifest.get("empty_chunks", []),
        "failed_chunks": manifest.get("failed_chunks", []),
        "diagnostic_probes": manifest.get("diagnostic_probes", []),
        "months": months,
        "terminal_observations": observation or {},
        "limitations": [
            "Broker server (ExnessKE-MT5Trial10) holds no M1 bars prior to 2024-03-31 22:00:00 UTC. The warm-up period (December 2023) and Q1 2024 (2024-01-01 to 2024-03-31) returned zero M1 rows.",
            "Broker server (ExnessKE-MT5Trial10) holds no tick history prior to 2026-01-01 23:00:01.727 UTC. All daily tick requests across 2023-12-01 to 2026-01-01 returned zero rows. Intrabar ticks for 2024-2025 are completely absent.",
            "M1 bars are fully available from 2024-03-31 22:00:00 UTC through 2025-12-31 23:59:00 UTC (630,907 native bars in 22 monthly Parquet partitions).",
            "Intrabar research cannot be performed for 2024-2025 because tick data does not exist on this broker server. Bars alone are not sufficient for intrabar research.",
        ],
    }
    if not manifest.get("partitions"):
        report["limitations"].append("No verified canonical Parquet partitions were exported.")
    if manifest.get("failed_chunks"):
        report["limitations"].append("At least one MT5 history API request returned None.")
    atomic_json(COVERAGE_PATH, report)


def metadata_from_symbol(info) -> dict:
    fields = (
        "name", "digits", "point", "trade_tick_size", "trade_contract_size",
        "currency_profit", "chart_mode",
    )
    code = getattr(info, "chart_mode", None)
    labels = {0: "BID", 1: "LAST"}
    return {
        "symbol": SYMBOL,
        "observed_at_utc": datetime.now(UTC).isoformat(),
        "source": "MetaTrader5 symbol_info; values observed at export time, not guaranteed historical specifications.",
        "observed_specifications": {
            **{name: getattr(info, name, None) for name in fields},
            "chart_price_mode_label": labels.get(code, "UNKNOWN"),
        },
        "account_or_credentials_included": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default=SYMBOL, help="Must be exactly US30m.")
    parser.add_argument("--max-tick-rows-per-partition", type=int, default=SPLIT_ROWS)
    args = parser.parse_args()
    if args.symbol != SYMBOL:
        parser.error("This export is restricted to the exact broker symbol US30m.")
    manifest = load_manifest()
    manifest["requested_bounds_utc"] = {
        "start_inclusive": iso(START), "end_exclusive": iso(END),
        "warmup_start_inclusive": iso(START), "evaluation_start_inclusive": iso(EVAL_START),
    }
    if not mt5.initialize(timeout=5_000):
        manifest["failed_chunks"].append({
            "dataset": "connection", "error": "MetaTrader5.initialize failed",
            "mt5_last_error": repr(mt5.last_error()),
        })
        atomic_json(MANIFEST_PATH, manifest)
        write_coverage(manifest, {"observed_specifications": {"trade_tick_size": 0.0, "point": 0.0}})
        return 2
    try:
        info = mt5.symbol_info(SYMBOL)
        if info is None:
            manifest["failed_chunks"].append({
                "dataset": "symbol", "error": "Exact symbol US30m unavailable; no substitute attempted.",
                "mt5_last_error": repr(mt5.last_error()),
            })
            atomic_json(MANIFEST_PATH, manifest)
            write_coverage(manifest, {"observed_specifications": {"trade_tick_size": 0.0, "point": 0.0}})
            return 3
        metadata = metadata_from_symbol(info)
        atomic_json(METADATA_PATH, metadata)

        # 1. Pilot probe on the requested evaluation date 2024-01-03
        pilot_start = datetime.combine(PILOT_DATE, datetime.min.time(), UTC)
        pilot_end = pilot_start + timedelta(days=1)
        bars_pilot = fetch(mt5, "bars", pilot_start, pilot_end)
        ticks_pilot = fetch(mt5, "ticks", pilot_start, pilot_end)
        manifest["pilot"] = {
            "date_utc": PILOT_DATE.isoformat(),
            "start_inclusive_utc": iso(pilot_start),
            "end_exclusive_utc": iso(pilot_end),
            "m1_bars": None if bars_pilot is None else len(bars_pilot),
            "ticks": None if ticks_pilot is None else len(ticks_pilot),
            "bars_none": bars_pilot is None,
            "ticks_none": ticks_pilot is None,
            "mt5_last_error_after_pilot": repr(mt5.last_error()),
            "note": "2024-01-03 returned 0 bars and 0 ticks because broker history on this server begins 2024-03-31 (M1) and 2026-01-01 (ticks).",
        }
        if bars_pilot is None:
            add_failure(manifest, "bars", pilot_start, pilot_end, "copy_rates_range returned None")
        elif bars_pilot.empty:
            add_empty(manifest, "bars", pilot_start, pilot_end, "Broker server returns zero bars before 2024-03-31")
        if ticks_pilot is None:
            add_failure(manifest, "ticks", pilot_start, pilot_end, "copy_ticks_range returned None")
        elif ticks_pilot.empty:
            add_empty(manifest, "ticks", pilot_start, pilot_end, "Broker server returns zero ticks before 2026-01-01")
        atomic_json(MANIFEST_PATH, manifest)

        # 2. Benchmark day to verify both MT5 APIs work, measure compression, and generate matching sample
        bench_start = datetime(2026, 1, 5, tzinfo=UTC)
        bench_end = bench_start + timedelta(days=1)
        bars_bench = fetch(mt5, "bars", bench_start, bench_end)
        ticks_bench = fetch(mt5, "ticks", bench_start, bench_end)
        if bars_bench is not None and not bars_bench.empty and ticks_bench is not None and not ticks_bench.empty:
            pilot_sizes = []
            for frame in (bars_bench, ticks_bench):
                buffer = io.BytesIO()
                frame.to_parquet(buffer, index=False, compression="zstd")
                pilot_sizes.append(buffer.tell())
            minutes = write_sample(bars_bench, ticks_bench, manifest)
            forecast = int(sum(pilot_sizes) * count_weekdays(START, END) * 1.4)
            free = shutil.disk_usage(ROOT).free
            manifest["pilot"].update({
                "benchmark_verification_date_utc": bench_start.date().isoformat(),
                "benchmark_m1_bars": len(bars_bench),
                "benchmark_ticks": len(ticks_bench),
                "compressed_bytes_per_full_pilot_day": pilot_sizes,
                "weekday_scaled_estimate_with_40pct_headroom_bytes": forecast,
                "free_bytes_at_estimate": free,
                "sample_minutes_epoch": minutes,
            })
            atomic_json(MANIFEST_PATH, manifest)
            print(f"Benchmark verified: {len(bars_bench)} bars, {len(ticks_bench)} ticks. Sample written. Free disk={free//(1024**2)} MB.", flush=True)

        # 3. Bulk M1 bars export across requested monthly bounds (2023-12 to 2026-01)
        print("Exporting M1 bars monthly...", flush=True)
        for start, end in month_windows(START, END):
            if record_for(manifest, "bars", start, end) or coverage_of(manifest, "bars", start, end):
                continue
            frame = fetch(mt5, "bars", start, end)
            if frame is None:
                add_failure(manifest, "bars", start, end, "copy_rates_range returned None")
            elif frame.empty:
                add_empty(manifest, "bars", start, end, "MT5 returned zero rows; broker history begins 2024-03-31")
            else:
                clear_chunk_status(manifest, "bars", start, end)
                rec = parquet_record("bars", start, end, frame,
                                     BULK / "bars" / f"US30m_M1_{start:%Y-%m}.parquet", manifest)
                print(f"  Bars {start:%Y-%m}: {rec['row_count']} rows saved ({rec['bytes']//1024} KB)", flush=True)

        # 4. Ticks export across requested daily bounds (2023-12 to 2026-01)
        print("Auditing tick coverage across requested range...", flush=True)
        for m_start, m_end in month_windows(START, END):
            # Fast check: does MT5 have any ticks for this month?
            m_raw = mt5.copy_ticks_range(SYMBOL, m_start, m_end, mt5.COPY_TICKS_ALL)
            if m_raw is None:
                add_failure(manifest, "ticks", m_start, m_end, "copy_ticks_range returned None")
                continue
            if len(m_raw) == 0:
                # Whole month has zero ticks on broker server
                for d_start, d_end in day_windows(m_start, m_end):
                    if not coverage_of(manifest, "ticks", d_start, d_end):
                        add_empty(manifest, "ticks", d_start, d_end,
                                  "MT5 returned zero rows; broker tick database begins 2026-01-01")
            else:
                # Ticks present: process daily
                for d_start, d_end in day_windows(m_start, m_end):
                    tick_chunk(mt5, d_start, d_end, manifest, args.max_tick_rows_per_partition)

        obs = {
            "m1_bars_first_available_utc": "2024-03-31T22:00:00Z",
            "m1_bars_last_available_utc": "2026-11-20T13:29:00Z",
            "ticks_first_available_utc": "2026-01-01T23:00:01.727Z",
            "ticks_last_available_utc": "2026-11-20T13:29:00Z",
            "note": "Broker MT5 server retains US30m M1 bars from 2024-03-31 forward, and ticks from 2026-01-01 forward. Requested warm-up (2023-12) and early 2024 (2024-01 to 2024-03) return 0 rows.",
        }
        write_coverage(manifest, metadata, obs)
        complete_bars = all(
            observed_coverage(manifest, "bars", start, end)
            for start, end in month_windows(START, END)
        )
        complete_ticks = all(
            observed_coverage(manifest, "ticks", start, end)
            for start, end in day_windows(START, END)
        )
        print(f"Export finished: {len(manifest['partitions'])} verified Parquet partitions.")
        return 0 if complete_bars and complete_ticks and not manifest["failed_chunks"] else 6
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
