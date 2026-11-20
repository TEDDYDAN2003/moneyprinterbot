# Verify the current trend-only bot and charts

The active policy is `bos_half_range_close_v4`. `reference.pine` is an exact copy of the shipped v4 source. Do not use the old chart-only v3 retest ZIP or v2 scanner oracle to validate it. [Release results](v4-verification.txt) · [Worked close-by-close cases](v4-cases.csv). No terminal or broker data is needed. All fixtures are synthetic; these tests verify decisions, not profitability.

From `MT5/` in the full checkout with Node 20+, Python 3.11+ with project dependencies installed, and `g++` (C++17):

```sh
(cd qa && npm ci --ignore-scripts)
node qa/pine_oracle.cjs qa/candles.json ../indicators/FC-Hull-Either-16-Structure.pine /tmp/hull-v4.json
python qa/check_port.py /tmp/hull-v4.json
python qa/check_chart_rules.py /tmp/hull-v4.json
python qa/check_bot.py /tmp/hull-v4.json
```

- PineTS 0.13.1 executes the deliverable with test-only plots: **29 scenarios, 28,936 closed-bar decisions, 49 numeric fields**. Separate prefix/forming-candle checks verify nothing is backdated. The last two mirrored histories include the *former* early cross, a strict CHoCH with no new cross, and an exact later retest; none creates an entry. The protected-swing fixtures also check a fresh matching cross on a true CHoCH (bar 45: **no entry**) and an eligible post-BOS trend cross (bar 92: **allowed**).
- The host shim runs the actual `.mq5` calculation and `OnCalculate` scheduler against the Pine fields, checking incremental/full history, forming candles, alert bars and diagnostic-only raw alerts, and retired-stage buffers. `trend_cases.hpp` adds **36 mirrored paths** for strict guard and inclusive midpoint, wrong-half and quote-edge vetoes, CHoCH close/touch rejection, next-open simulation, gaps, and independent opposite exits.
- Independent rule assertions reconstruct each band's inequalities. The Python comparison checks **17 structural/raw fields** and, where the bot and chart options match, the actual pre-slot stop/EMA/entry gate. Broker fills are deliberately not compared to OHLC proxy fills.
- From the repo root, `python -m pytest -q` additionally exercises the production scanner loop against a fake terminal, including old private timing configs, no index-zero rate reads, no broker requests on former reversals or vanished live crosses, first post-close quotes, per-leg rechecks, funding, old-position exits, ladders and restart safeguards. This does not launch a real scanner.
