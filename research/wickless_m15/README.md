# Wickless M15 · test record

No parameters were tuned on these runs. The bot implements the requested entry rules; the following is a behavioral check, **not evidence of a profitable edge**.

## Two different denominators, not two broker statements

| EURUSDm, October 2026 | Setups | Respected signals | Simulated entries | TP / SL | Result |
|---|---:|---:|---:|---:|---|
| Primary: ordered public bid/ask, first post-close quote | 100 | 57 | 27 | 13 / 14 | **−$11.40** at 0.01 lot before fees; −1.06738R |
| Diagnostic: complete M15 OHLC buckets, ideal rejection close | 88 | 48 | 21 | 10 / 11 | **−1.00R** before all costs |

**Quote test.** One pair only. 1,038,972 October Exness archived bid/ask updates, 2,099 non-empty M15 buckets. September quotes provide 1,800 completed pre-start bars for structure only. Fixed 0.01 lot, EURUSD 100,000 units/lot, 0.00001 tick, 0.0001 pip, spread cap 3 pips and gross risk cap $25. Prices, source order (including equal timestamps), coverage, OHLC and archive hashes are validated. The production `Engine` and read-only `execute()` admission/price plan are used, never a connected terminal. No future quote in a closing decision. Existing brackets are evaluated before a new entry on the same quote. Buy ask / sell bid on entry; SL on first crossing executable bid/ask, TP at the limit without favorable price improvement. **28** suggestions were blocked by an open position and **2** were too late. There was no open position at end. Currency PF **0.54527** differs from a count-based ratio because structural stops and cash risk vary at fixed lot size. **No modeled commission, swaps, network or polling latency, broker rejection, or live account fills.** Real returns could be worse. This is one known month and instrument, not a holdout or portfolio estimate.

[Quote source URLs and pinned hashes](quote-smoke.json) · [every simulated price, timestamp, R and P/L](quote-ledger.csv). Download the two archives listed there and reproduce, from the repo root:

```sh
python research/wickless_m15/quote_smoke.py September.zip October.zip /path/to/output
```

**OHLC test.** The delivered [M15 bars](EURUSDm-M15.csv) come from the earlier EURUSDm October M1 file. Only 2,007 complete 15-row UTC quarters are used; 92 nonempty but incomplete quarters are omitted. This test starts with neutral structure, not September warm-up. A subsequent OHLC bar touching both levels counts as a stop unless its open establishes the outcome. Entries are ideal close prices, without bid/ask, broker caps, spread, or commissions. Gaps are modeled only from available bars. Do not interpret differences between these runs as purely the cost of the spread. [Provenance](source.json) · [result](smoke.json) · [ledger](close-ledger.csv).

```sh
python -m bot.wickless replay --config wickless.example.json --symbol EURUSDm --bars research/wickless_m15/EURUSDm-M15.csv --tick-size 0.00001 --out close-ledger.csv
```

## Software checks

Release check: **549 repository tests passed** (77 focused Wickless tests); **34 separate Hull Journal tests passed**. Both archived replay summaries and both ledgers reproduced byte-for-byte. No scanner was launched against a broker.

- `tests/test_wickless_strategy.py` and `tests/test_wickless_terminal.py`: both sides of each zone edge; cap and stop vetoes; neutral trend, BOS/MSS, no future pivots; mirrored signals; expiry including final bar; no same-bar entry; once-only selection of overlapping origins; outages; read-only/armed separation; exact lots and symbol allowlist; confirmed/slipped fill brackets; shared scanner locks; durable ambiguous-send recovery and fake MT5 integration. Full repository suite also includes unchanged Hull behavior.
- PineTS `0.13.1`: compare the shipped Pine strategy with Python on **3,000 closed M15 bars** (base, mirrored, changed tolerances/window/buffer) × **11 fields**, including setup SL, protected level, surviving zones, entry bar and entry brackets; compare both actual plotted labels (**172 BUY, 181 SELL**). Also check no suggestion/setup on forming buy and sell examples and reject M5. This is an independent Pine runtime, not an attestation from TradingView’s hosted compiler or strategy emulator.
- Chromium: desktop and mobile layout, local broker switching, reload, per-row sizing, exact-selection copy, new/edit/remove pairs, JSON import validation, validated JSON download, observer-only handoff, and restricted-storage/clipboard fallback. No remote broker or user data used.

Optional reproduction (install the repo first; Node dependencies for QA only):

```sh
python -m pytest -q tests/test_wickless_strategy.py tests/test_wickless_terminal.py
npm ci --prefix MT5/qa
python tests/wickless_qa/fixture.py /tmp/wickless-fixture.json
NODE_PATH="$PWD/MT5/qa/node_modules" node tests/wickless_qa/parity.cjs /tmp/wickless-fixture.json
# Browser QA: install Python playwright + Chromium, serve only WicklessDesk locally, then:
python tests/wickless_qa/browser.py http://127.0.0.1:8035
```
