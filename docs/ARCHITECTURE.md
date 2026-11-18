# Architecture

**Current zone pilot:** see [implemented strategy and runtime contracts](ZONE_PILOT.md). The notes below describe the earlier research foundation.


The system follows one deterministic path:

`data -> completed bars -> features -> labels/models -> decision -> risk governor -> execution -> management -> reports`

Batch backtests, paper trading, and live MT5 execution share the same feature, decision,
risk, and management contracts. The first redesign target is a shared multi-timeframe
pipeline for M1, M3, M5, M10, and M15 with strict completed-bar boundaries.
