# US30m MT5 Historical Market Data Export & Verification

This directory contains the export tooling, verification reports, symbol specifications, and benchmark samples for the Hull crossover forecasting study on the exact broker symbol `US30m`.

The export is strictly read-only: it does not place or manage orders, change terminal settings, switch accounts, or access private credentials.

---

## 1. Scope & Execution Rules

- **Exact Broker Symbol**: `US30m` only (no substitutes like `US30`, Dow Cash, or DJI).
- **Target Period**: `2024-01-01 00:00:00 UTC` to `2026-01-01 00:00:00 UTC`.
- **Warm-Up**: `2023-12-01 00:00:00 UTC` to `2024-01-01 00:00:00 UTC` (marked separately).
- **Splits**:
  - `training`: 2024-01-01 through 2024-12-31
  - `validation`: 2025-01-01 through 2025-06-30
  - `untouched_final_test`: 2025-07-01 through 2025-12-31
- **Timestamp Integrity**: Preserves MT5's integer timestamps (`time` in seconds for bars, `time_msc` in milliseconds for ticks). Timezone-aware UTC requests; no local clock offset applied.

---

## 2. Exact Rerun Command

To rerun or resume the export, open PowerShell in the repository root with MT5 running and connected to your demo account:

```powershell
.\.venv\Scripts\python.exe -m pip install pyarrow
.\.venv\Scripts\python.exe .\research\hull_early_warning\mt5_export\export_mt5.py
```

---

## 3. Findings & Coverage Summary

### M1 Bars (Native `copy_rates_range`)
- **Coverage**: `2024-03-31 22:00:00 UTC` through `2025-12-31 23:59:00 UTC`.
- **Row Count**: **630,907 native M1 bars** across 22 monthly Parquet partitions.
- **Location**: `research/hull_early_warning/mt5_export/bulk/bars/` (Git-ignored).
- **Total Bulk Size**: **13,478,297 bytes (~13.48 MB)**, compressed with Zstandard.
- **Missing Range**: `2023-12-01` to `2024-03-31 22:00:00 UTC` returned zero rows from the broker server (`ExnessKE-MT5Trial10`).

### Ticks (Original `copy_ticks_range` with `COPY_TICKS_ALL`)
- **Server Limit**: The broker tick server maintains tick history beginning on **`2026-01-01 23:00:01.727 UTC`**.
- **Historical Availability**: **Zero historical ticks** exist on this broker server for the 2023–2025 requested period.
- **Daily Audits**: Every day from `2023-12-01` through `2025-12-31` was probed; all 760+ daily queries returned 0 rows.

### Benchmark & 1-Minute Reconstruction Quality
- **Benchmark Date**: `2026-01-05` (first active trading day with tick data).
- **Bars & Ticks Verified**: 1,380 native M1 bars and 200,840 raw ticks.
- **Reconstruction Accuracy**: **100.00%** (1,380 / 1,380 bars matched native M1 OHLC within 1 trade tick of 0.1).
- **Quote Quality**: 0 invalid quotes (bid <= 0 or ask <= 0), 0 crossed quotes (bid > ask), 0 out-of-order timestamps.

---

## 4. File Manifest & Artifacts

- `export_mt5.py`: Resumable, verified exporter and coverage reporting script.
- `symbol_metadata.json`: Export-time observed specifications (`US30m`: digits=1, point=0.1, tick_size=0.1, contract_size=1.0, currency=USD, chart_mode=BID).
- `manifest.json`: SHA-256 hashes, byte sizes, bounds, and row counts for all 22 partitions and benchmark samples.
- `coverage_report.json`: Month-by-month bar counts, timestamps, weekend closure classifications, and exact broker limitations.
- `sample/`:
  - `training_day_bars.parquet`: 5 native M1 bars from benchmark day (`2026-01-05 00:00`–`00:04` UTC).
  - `training_day_ticks.parquet`: 616 matching raw ticks from those 5 minutes.
  - `README.md`: Sample documentation and verification notes.
- `bulk/`:
  - `bars/`: 22 monthly Parquet files (`US30m_M1_2024-03.parquet` through `US30m_M1_2025-12.parquet`).

---

## 5. Final Handoff Verdict

**Verdict: PARTIAL with exact limitations**

### Limitations & Next Steps:
1. **Intrabar Research Limitation**: Intrabar tick data cannot be obtained for 2024–2025 from this MT5 server because the broker does not store ticks prior to 2026-01-01. **M1 bars alone are not sufficient for intrabar research.**
2. **M1 Bar Research**: For candle-level research (M1 Hull crossover signals, bar closes, swing levels), continuous high-quality data is **fully ready and available** from `2024-03-31` through `2025-12-31` (630,907 bars).
3. **If Historical Ticks Are Required**: Intrabar tick data for 2024–2025 must either be requested directly from the broker's institutional archive or imported from an external tick archive (e.g. Dukascopy / Tick Data) into MT5.
