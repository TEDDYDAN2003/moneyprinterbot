# Assumptions

**Current zone pilot:** see [implemented strategy and runtime contracts](ZONE_PILOT.md). The notes below describe the earlier research foundation.


- The existing implementation was explicitly removed before the redesign.
- The new system is XAUUSD-only, with broker symbol suffixes configured rather than hard-coded.
- One shared feature pipeline and model family will support M1, M3, M5, M10, and M15, with timeframe-specific heads or calibration where walk-forward evidence supports it.
- The default execution mode is paper. Live execution requires every README acceptance gate.
- Historical logs and reports are evidence for audit, not training labels by themselves; labels must be regenerated from timestamped market data with costs.
