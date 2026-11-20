# Two distinct references: v3 charts vs. v2 demo scanner

None of these development tools is needed to install the indicator. All candles are **synthetic**. No account, ticks, credentials, orders, profitability study, or native MetaEditor / TradingView compiler is involved.

## Chart-to-chart · `momentum_retest_v3`

From the `MT5` directory with Node 20+, Python 3.11+ and a C++17 `g++`:

```sh
(cd qa && npm ci --ignore-scripts)
node qa/pine_oracle.cjs qa/candles.json ../indicators/FC-Hull-Either-16-Structure.pine /tmp/chart-v3.json
python qa/check_port.py /tmp/chart-v3.json
python qa/check_chart_rules.py /tmp/chart-v3.json
```

- The oracle runs the **actual Pine** in pinned PineTS 0.13.1, adding test-only plots. There are 29 scenarios and **28,936 closed-bar decisions, 62 numeric fields each**. Prices use a floating tolerance in the host comparison; discrete decisions must agree. Alert counts, prefixes, and forming bars at the watch, confirmation and retest are separately checked. The mirrored `watch-buy` and `watch-sell` scenarios use actual Hull curves over two deterministic OHLC paths (derived in the oracle, not injected labels).
- `check_port.py` runs the **actual delivered MQL5** calculations and `OnCalculate` schedule using a Linux C++ shim for native APIs. It checks chronological vs incremental history, repeated forming updates, alert gating and exposed buffers. `retest_cases.hpp` adds **31 paths × both directions = 62 hand-specified cases** by controlling the sign of the Hull cross independently of OHLC. These cover midpoint/guard edges, allowed and disallowed watches, cancellation/rearming, wick/equality/strict CHoCH, confirmation with or without a matching cross, pending vs filled, discontinuities, stop/slot vetoes, independent exits, price improvement and ambiguous intrabar barriers.
- `check_chart_rules.py` separately asserts rule invariants against the Pine output. On the mirrored showcase paths, bars 410/418/420 respectively arm the watch, close through its guard **without a new cross**, and touch the frozen limit after an untouched bar. No same-candle fill even though the confirmation candle itself sweeps its future limit. These are decision checks, **not independent trades** or a return estimate.

## Archived scanner reference · `protected_swing_v2`

The charts-only ZIP omits `check_bot.py` and `reference.pine`; use the full checkout for this section. `reference.pine` is deliberately the **old v2 chart**, preserved for the unchanged Python scanner's closed-bar reference. Do not install it as the new indicator. Do not use a v3 output file for the scanner test. In a full repository checkout with bot dependencies installed:

```sh
node qa/pine_oracle.cjs qa/candles.json qa/reference.pine /tmp/scanner-v2.json
python qa/check_bot.py /tmp/scanner-v2.json
```

This second test checks 16 raw/structure fields on 28,094 v2 bar decisions, the protected minor-swing regressions and frozen quote vetoes. It does not claim the v2 scanner takes the same entries as the v3 charts, or that the historical chart reconstructs a live scanner fill.
