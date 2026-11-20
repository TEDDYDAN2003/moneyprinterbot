# US30m Sample Dataset (Matching Bars & Ticks)

This folder contains a small, matching sample of native M1 bars and original ticks for `US30m` from the pilot verification benchmark day (`2026-01-05 00:00:00` to `00:04:59` UTC).

> **Note on requested period (2023-12 through 2025-12)**:
> The terminal's connected broker server (`ExnessKE-MT5Trial10`) retains historical tick data starting on `2026-01-01 23:00:01.727` UTC, and native M1 bars starting on `2024-03-31 22:00:00` UTC. Consequently, requests for the earlier pilot day (`2024-01-03`) returned zero ticks and zero bars. To verify that both APIs work properly and test reconstruction accuracy without fabricating data, the pilot benchmark verified the first active trading day with ticks (`2026-01-05`).

## Files

- `training_day_bars.parquet`: 5 native M1 bars (`2026-01-05 00:00:00` to `00:04:00` UTC).
  - Columns: `time`, `open`, `high`, `low`, `close`, `tick_volume`, `spread`, `real_volume`, `time_utc`, `research_split`.
- `training_day_ticks.parquet`: 616 raw ticks from those exact 5 minutes.
  - Columns: `time`, `bid`, `ask`, `last`, `volume`, `time_msc`, `flags`, `volume_real`, `time_utc`, `research_split`.

## Verification & Reconstruction

- Native M1 OHLC and tick-reconstructed bid OHLC match with **100.00% accuracy** within 1 trade tick (0.1).
- Timestamp ordering and duplicate millisecond ticks are strictly preserved.
