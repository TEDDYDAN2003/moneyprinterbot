# Candle / retest scanner • observe first

> **Archived operating guide.** `trade` and `trade-all` now run the [Hull M1/M2/M5 scanner](HULL_DEMO.md), not the strategy below. The historical replay commands remain available.

**Keep `run.mode: paper` and `zones.price_action.demo_opt_in: false`.** The implemented rules can put the actual stop beyond the reaction candle without silently increasing the lot or risk allowance. The independent-feed study did **not** establish a profitable strategy. [Results, trade examples and limitations](../research/candle_retest/REPORT.md) · [Original zone engine and per-symbol configuration](ZONE_PILOT.md).

## What the scanner knows and when

- First require a *closed* reaction at an already-confirmed M1, M5 or M15 level. Price must respect a pullback, sweep/reclaim a pool, or break and then retest. A long green candle in the middle of a range is not a signal. BOS, swing labels and trendline proximity are context, not an excuse to skip the level. Pools are inferred, not reported resting orders.
- Tag the completed M1 real body, engulfing, upper/lower rejection wick, inside bar and doji. Compare it with the immediate predecessor, log whether the predecessor touched the **same previously known band**, plus the most recent finished source-frame pattern. No unfinished M5/M15 candle or future swing is used. Definitions are in `src/bot/zones/candles.py`. The primary shadow policy records but does **not** require an engulf/rejection to enter; making it mandatory did not improve the measured win rate. The `pattern_*` variants let you test that filter explicitly.
- Freeze one setup for up to 180 s. In `reaction_managed`, ordinary reactions wait for a quote inside the original band; a sweep/reclaim waits for a return into the **already swept area**, from the far edge to the observed extreme. Do not chase the first impulse, retrospectively fill a past touch, blindly fade an accepted break, or enter once invalidation/objective has traded. Expiry, opposite structure, stale quotes, data problems and session gates can end a watch. Some watches will produce no trade.
- For a sell put the hard SL one tick above the farther of the reaction high and known adverse excursion/band edge, then add the **entry spread** and round outward for an ask-triggered short stop. For a buy put it one tick below the farther low: longs stop on bid. Use the actual wick SL, not the fixed cash stop with a different label. The lot is unchanged; if the position would risk more than `sl_usd` or violate broker distances, wait, then skip. A static ask stop cannot guarantee protection of a bid candle if spreads widen later. Do not move it out once in the trade.
- Fixed-target arms use +$10; next-zone arms need a known opposing level or configured round-number objective providing **at least** the reference target. A managed trade keeps the hard SL/TP. Each M1 close checks contrary BOS or contrary engulf/rejection with adverse pressure, or two successive adverse pressure readings (threshold 0.30). At quote polls, after marked profit reached $10, give back 40% of the recorded peak and exit at the current quote. This is not a break-even guarantee. Missing ticks, disconnects and holidays can prevent software exits. No mandatory partial close or unbounded winner is assumed.

## Send the missing broker data (read-only)

On the **Windows machine with your broker's MT5 terminal** logged in, from this repository:

```powershell
python -m pip install -e . MetaTrader5 tzdata
python -m bot.cli export-ticks --symbol XAUUSD --start 2026-10-28 --end 2026-11-12 --out data/XAUUSDm_ticks.csv.gz
```

`XAUUSD` is the config key; its `broker_symbol` is `XAUUSDm`. Change the latter if your suffix differs. The extra day on Oct 28 supplies warm-up. The date interval is UTC, end exclusive. The exporter issues **no orders**, reads no account identifiers and writes UTC `timestamp,bid,ask` plus a `.manifest.json` with symbol, point, tick size, contract size, coverage, empty query hours and SHA-256. Upload those two files, not account credentials. Empty hours can be normal closures or missing history; verify coverage. If you already have exact millisecond UTC bid and ask ticks, upload those instead. An M1 bid candle CSV, trade list or screenshot cannot establish which quote came first inside a minute.

Cross-platform, after installing the project, compare your broker's exact same dates and rules:

```sh
python -m bot.cli candle-test --symbol XAUUSD --ticks data/XAUUSDm_ticks.csv.gz --start 2026-10-29 --end 2026-11-12 --policy all --out reports/candle_replay_broker
python -m bot.cli candle-test --symbol XAUUSD --ticks data/XAUUSDm_ticks.csv.gz --start 2026-10-29 --end 2026-11-12 --policy reaction_managed --stop-fill first_quote --out reports/candle_replay_broker_crossing
```

`window/summary.json` and individual `window/*.csv` report fills, affordable vs rejected checks, entry/exit times, classified candle, wick, actual SL, TP, modeled risk, observed P&L and exit reason. No fill = undefined PF/win rate. `first_quote` is a gap sensitivity, not added slippage/commission. Stop at the first observed crossing quote; targets still fill at their limit. Check actual broker specifications, session times and protection distances before interpreting discrepancies. Generic tick replay cannot reproduce terminal acceptance, execution latency, queueing or counterparty fills.

## Shadow forward observation

`python -m bot.cli trade-all --config config.yaml` on Windows observes the symbol list; it does not place orders. Logs under `reports/zone_runtime/<account-hash>/shadow.jsonl` include `candle_decision`, `watch_decision`, `watch_wait` on reason change, `return_decision`, `shadow_exit` and `shadow_censored`. Startup history warms the chart but never generates retroactive orders. Watch and paper-position memory are local to the process; restart discards unfinished simulations. Per-symbol shadow P&L is an indicative hypothetical leg, not a portfolio statement. Software checks run each configured poll (2 s by default; `--poll-seconds` overrides); broker attachments, where present on demo, are not dependent on polling. Missing verifiable tick history cancels a pending watch; shadow returns are not filled at a low/high seen between two polls. Manage open positions before changing mode, caps, symbols or attempting a restart.

`zones.price_action.policy` chooses the profile; `ttl_seconds`, `buffer_ticks`, `poll_seconds`, `adverse_momentum` and `giveback_fraction` are explicit. `baseline` preserves the v1 immediate scanner and its `zones.rules` switches. Challenger profiles choose their own stop/exit behavior; legacy `zones-replay` remains the v1 experiment. Only XAUUSD is enabled; each symbol still owns its lot, per-leg TP/SL, spread cap and batch count under `zones.symbols`. Keep one leg when $5 is the *total* budget. Multi-symbol portfolio performance was not tested. Demo execution remains behind all earlier broker/account guards plus a separate opt-in for challenger rules. No live-money path and no connected-terminal test in this delivery.
