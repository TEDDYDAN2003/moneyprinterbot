# Gold zone-entry experiment • 18 November 2026

**Decision: keep this version in shadow mode.** A sweep/retest engine was implemented and tested, but the requested 0.09-lot, $10-target, $5-stop configuration did not show a profitable edge. On the later October 29–November 11 sample it went **1 win / 8 losses**, even with zero commission and zero slippage. The loss was smaller in dollars than the matched legacy control, **not** smaller per trade.

## What the audit found

| Finding in original `main` (`35782d5`) | Consequence / change |
|---|---|
| `src/bot/decision/market_aware.py::evaluate_market` accepts `pullback_confirmed OR continuation_confirmed`. It computes a weighted, uncalibrated directional score. | A continuation can enter away from a tested level. New zone logic cannot enter on standalone momentum. The older scorer is preserved as a control; the demo losses are not proof of a single root cause. |
| `config.yaml` set M1 `batch_size: 5`, 0.05 lot per leg, $2.50 stop per leg and $25 **batch** target. Cash-mode risk checks can bypass the friction-fraction veto in `demo_runner.py`. | That is one correlated bet split five ways, not five independent confirmations. New defaults: **one leg**, fixed size without fallback, per-leg cash barriers, aggregate risk checks. |
| Legacy `run.mode: auto` could arm real/contest accounts despite a demo-oriented runbook. Old scheduled/kill paths could reach positions beyond the initiating worker. | Paper default, one scanner, explicit demo-only arming; only the new strategy's allowlisted positions can be closed. Legacy runner is shadow-only. |
| The old generic backtester requires a supplied signal callback, uses one OHLC price for both sides and lacks this cash/contract execution contract. The MT5 history downloader discarded the broker spread column. | Dedicated replay shares decision and cash-price code with the scanner. It preserves quote side, next-quote timing and real tick order, and the downloader now retains spread. |
| `reports/adaptive_state_m1.json` records 18 wins/114 losses. The partial training report describes M1 AUC ≈0.510 and names local `data/bars/xauusdm_m1.parquet`, which is not tracked. The current scorer does not automatically train a new model from those executions. | Those are archived observations, not the new backtest. The two tracked CSVs are selected trades and a timeframe summary, not raw prices. Nine screenshots illustrate levels/channels, not an ordered bid/ask path. The TradingView gold panels themselves show 17 TP vs 19 SL on M5 and 75 vs 88 on M1; they are not MT5 bot fills or evidence for this 2:1 cash strategy. |

## Test contract

- **Data:** an independent public XAUUSD bid/ask tick series, not the user's `XAUUSDm` broker history. Source: [3](https://github.com/Touhid-HZ/xauusd-tick-data-2026), pinned revision `3ef0f9a5940898a3f690140481500730a3fe178a`, files `XAUUSD_2026_w40` through `w45`. SHA-256 hashes are in `reproduce.py` and each summary. No raw tick files or broker credentials are published here.
- Actual coverage: **10,634,415 ordered quotes**, **41,340 observed M1 bars**, Oct 1 through Nov 11, 2026 UTC. Early: 5,640,473 ticks / 27,600 bars. Later: 4,993,942 ticks / 13,740 bars. Compare chronologically; don't randomize or re-fit on the later sample. It is a historical validation slice, not an independent forward demo or proof of future performance.
- Bid M1 generates completed source levels. One decision on each M1 close; first observed quote of the next minute (within eight seconds) is eligible. Buy at ask, exit at bid; sell at bid, exit at ask. Spread enters **once**, via executable sides. No commission, no added slippage, no latency penalty. Default barrier fills are ideal: TP/SL at their level even if the next quote jumps through. Tick order resolves TP-first vs SL-first; no candle guessing in these results. The $10/$5 amounts become approximately +$10.008/−$4.995 after rounding a 100-oz contract to 0.001 price ticks. Real fills are not guaranteed to match.
- Default max spread 0.300 gold price units; one 0.09-lot leg; common account limits of $30 daily loss and $75 peak drawdown halt new entries. Each window starts flat at $10,000, uses prior signal history if available, and gets a fresh guard. The guard marks open positions at minute boundaries in replay; **reported max drawdown below is realized peak-to-trough USD**, not worst intratick open-equity drawdown. A halt stops subsequent entries; it does not guarantee that the last loss stops equity at exactly $75 down.
- **Matched legacy control:** original `evaluate_market` for the enabled M15/M5/M1 workers, deterministic M15→M5→M1 priority at one quote per minute, with the same barriers, costs, session/risk limits and one leg. No worker races, tick-by-tick repeat submissions, five-leg replication or adaptive loss penalty. It's a comparison of entry policies under common execution, **not an exact reconstruction of the historical running bot**.
- Rules were not optimized on these displayed returns. No volume scaling, no win-streak sizing. Broker chart spreads/fills, session hours, data gaps, leverage and sampling can change results. Multiple simultaneous symbols were implemented in the monitor but **not** portfolio-backtested; no performance claim is made for EURUSD or USDJPY.

## Results (USD)

| Window (UTC, inclusive dates) | Entry / exit policy | Wins / trades | Win rate | Profit factor | Net P&L | Max realized DD |
|---|---|---:|---:|---:|---:|---:|
| Oct 1–28 | Legacy entries / fixed cash | 4 / 28 | 14.3% | 0.33 | −79.85 | 79.85 |
| Oct 1–28 | Zone entries / fixed cash | 2 / 13 | 15.4% | 0.36 | −34.93 | 44.96 |
| Oct 1–28 | Zone entries / hold to next zone | 2 / 13 | 15.4% | 0.90 | −5.69 | 44.96 |
| Oct 29–Nov 11 | Legacy entries / fixed cash | 9 / 43 | 20.9% | 0.53 | −79.76 | 79.78 |
| Oct 29–Nov 11 | **Zone entries / fixed cash** | **1 / 9** | **11.1%** | **0.25** | **−29.95** | **34.97** |
| Oct 29–Nov 11 | Zone entries / hold to next zone | 1 / 9 | 11.1% | 0.33 | −26.61 | 34.97 |

The legacy control hit the drawdown block in both windows. The zone engine lost an average **$3.33 per executed trade** in the later sample versus **$1.85** for that control. Its lower dollar drawdown is consistent with taking less exposure, not evidence of better entries. Holding for the next objective increased the later winner from $10.008 to $13.347; the eight losses remained. Enforcing a structural-stop requirement **at the same 0.09 lot and $5 cap** produced **zero trades in both windows**; win rate and profit factor are undefined, not 0% wins or infinity.

### Where did the later trades go?

Of 1,246 candidate reactions (468 ordinary pullbacks, 330 BOS retests, 237 sweeps/reclaims, 211 other accepted-break retests), 1,211 first failed the spread cap, 11 the schedule, 8 the room-to-target check, 4 the freshness test and 3 the entry-location test. **9 executed.** Only 192 of 13,740 opening quotes met the cap, independent of signals; median opening spread was **0.539**. Actual `XAUUSDm` quotes are therefore an important missing input. The filter was **not** loosened just to create more trades.

| Later executed setup | Wins / trades | Net |
|---|---:|---:|
| Confirmed BOS + later retest | 0 / 3 | −$14.985 |
| Respected pullback | 0 / 3 | −$14.985 |
| Sweep + reclaim | 1 / 3 | +$0.018 |

Three observations per group are not enough to select a winning setup. The tiny positive result in the sweep row is tick rounding, not a demonstrated edge.

## Why even zero commission does not fix the stop

0.09 lot × 100 ounces = **9 ounces**. A $5 loss corresponds to about **0.556 price units from entry**, and $10 to **1.111**. With a 0.240 spread the position starts $2.16 underwater; the executable exit quote can move only **about 0.316** farther against it before the stop. Neither commissions nor slippage are in that calculation.

**Example, November 4 at 04:58 UTC:** after an M5 bullish BOS retest, the simulated long entered at ask **3953.496**. Its cash stop was **3952.941**. One tick beyond the retest wick was **3948.845**, implying **$41.86** of structural risk at the requested lot. The cash stop triggered first. It was protecting the cash budget, not expressing invalidation of that retest.

Across all nine later entries **0/9** cash stops cleared the setup's adverse wick/far band edge. The median modeled loss at actual structural invalidation was **$25.13**, not $5. Indicative size at that median distance is about **0.018 lot before broker step rounding**; that's a diagnostic for this sample, not a new lot recommendation or evidence a smaller lot would win. The bot keeps **0.09** unless the user changes it.

## Next experiment

1. Collect the broker's actual timestamped bid/ask ticks, or at least its bid M1 candles with spread points, point size and server timezone. Compare against the independent-feed findings on the same dates. Enable decision journaling in observation mode; audit where the detector called a sweep vs an accepted break **without hindsight**.
2. Keep one leg. To keep 0.09 lot *and* the $5 cap, test waiting for a **second, closer pullback** after confirmation, expiring the setup if the level breaks or the retest never comes. If variable size is acceptable in a **separate** experiment, fix the stop just beyond invalidation first, then size down to the $5 risk budget and round down to broker lot step. Never silently widen the $5 stop or change the primary experiment's lot. Neither alternative has been validated by this comparison.
3. Freeze rules before another chronological test. Require positive expectancy after observed spread, PF above 1 in multiple independent windows, tolerable drawdown and a meaningful number of **independent setups**, not multiple legs of one signal. Form a fresh broker-demo forward sample; only then compare another explicitly selected symbol with its own contract/limits. More instruments or batch legs can multiply losses as easily as opportunities.

## Reproduce & audit

From repo root, `python -m pip install -e .` then `python research/zone_pilot/reproduce.py --download`. This downloads the six pinned inputs (~76 MiB compressed), checks hashes, and runs the eight comparisons. No MT5 or live connection. Outputs default to `reports/zone_replay_public/`.

Committed evidence under [results/](results/): each run has `summary.json` and `trades.csv`. See the [later fixed-cash ledger](results/later-zones/trades.csv), [later control](results/later-legacy/summary.json), [hold variant](results/later-hold/summary.json) and [structural compatibility diagnostic](results/later-structural/summary.json). [Operating instructions](../../docs/ZONE_PILOT.md) cover broker CSV formats, account guards and symbol configuration.
