# Protected-swing v2 · offline parity

No QA tool is needed for a normal MT5 installation. From the `MT5` folder on a development machine with Node, Python 3.11+ and `g++`:

```sh
cd qa && npm ci --ignore-scripts && cd ..
node qa/pine_oracle.cjs qa/candles.json qa/reference.pine /tmp/hull-oracle.json
python qa/check_port.py /tmp/hull-oracle.json
# Full repository checkout, with bot dependencies installed:
python qa/check_bot.py /tmp/hull-oracle.json
```

Both candle sets are **synthetic**. The oracle executes the actual Pine in PineTS; `check_port.py` uses the delivered MQ5 calculations and OnCalculate scheduler via C++ adapters (44 fields, incremental/full replay, no intrabar decisions or attachment alert). `check_bot.py` checks the production Python lane's raw crosses, structural bias, break, protected level, observed vs confirmed tip, pullback evidence, frozen bounds and eligibility on identical bars (16 exact fields), plus hand-specified outcomes. Native chart simulation and broker deals are not the same thing.

`protected_cases.json` adds a 120-bar bearish incident-shaped fixture, its bullish mirror (mirrored about 3680), and two CHoCH next-open equality veto branches. Samples 45/46 exercise immediate CHoCH eligibility and the next-open check. Sample 92 of the sell fixture crosses down at 3677.159 under the 3682.840 guard after a minor 3675.945 high was breached. No future LL is needed. These are not historical Exness candles. There are 27 scenarios and 28,094 per-bar comparisons per implementation; this is neither a native EX5 compilation nor a profitability test.
