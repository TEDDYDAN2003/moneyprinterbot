# Dynamic Hull and chart checks (offline only)

From the repo root, with the project installed and a C++17 `g++` on PATH:

```sh
python MT5/qa/check_dynamic_lanes.py
python -m pytest -q tests/test_hull_cross_candle.py tests/test_hull_failed_mss.py tests/test_hull_journal_bridge.py
```

The shim compiles the calculation from the [current monitor](../FC-Dynamic-Hull-Lanes.mq5). It compares confirmed raw crosses, trade-grid stops, first-cross skips/waivers, intrabar exit-only projections and broker-D1-aligned M45 bars to Python. It also exercises three-lane updates and attach/duplicate alert suppression. It does not compile an EX5, connect to a terminal, or model profit. See [native installation](../DYNAMIC-LANES.md) for MetaEditor.

`package.json` / `package-lock.json` pin the optional PineTS dependency for the supported TradingView, Free Chart and Wickless code contracts. See the [chart study guide](../../docs/HULL_CHART_REPLAY.md). A chart study is not the Python order manager.
