# Zone pilot • quickstart & operating rules

> **Current default:** `reaction_managed` in shadow. [Candle/retest rules and the XAUUSDm tick exporter](CANDLE_RETEST.md). The immediate-entry rules and `zones-replay` below describe the v1 control. For the challenger, `--arm-demo` alone is not sufficient.

**Experiment only.** The fixed-cash zone rule lost money on the independent gold replay. Keep `run.mode: paper`. There is no live-money execution path in the CLI. No Pine indicator, TradingView subscription, external signals, hosted database, or ML model is needed. See [the measured comparison](../research/zone_pilot/REPORT.md) before considering a demo order.

## Replay your broker first (no terminal required)

From the repository root, using Python 3.11+:

```sh
python -m pip install -e ".[test]"
python -m bot.cli validate-config
python -m pytest -q
python -m bot.cli zones-replay --symbol XAUUSD --csv data/XAUUSDm_M1.csv --out reports/zone_replay_gold --decisions
```

- `--symbol` is the key in `zones.symbols`, not necessarily the terminal name. Offline replay can also study a configured but disabled symbol; the MT5 scanner cannot trade it until you enable it.
- Best input: `--ticks week1.csv.gz week2.csv.gz` (chronological files, `timestamp,bid,ask`; ISO UTC, millisecond resolution). No assumed spread is substituted for observed quotes unless you *explicitly* pass the counterfactual `--spread-override`.
- Alternatively, M1 bid candle CSV: `timestamp,open,high,low,close[,volume,spread]`. Timestamps are **minute opens**. `spread` means MT5 **points**, not pips/price units; the prior completed bar's value is used, not the eventual statistic of the entry minute. Where no spread exists, the configured `assumed_spread` is used. Supply all four `ask_open,ask_high,ask_low,ask_close` for observed ask OHLC. OHLC cannot reveal order inside a bar: if both barriers are touched, replay chooses the stop; a known opening quote is handled first.
- MT5 `<DATE> <TIME> <OPEN> ...` tab exports are accepted. For naive broker-clock timestamps specify e.g. `--source-tz Etc/GMT-2` **only if correct for that export**. UTC is the default. For repository-style `data/bars/xauusdm_m1.parquet`, pass the path to `--csv` and install `pyarrow`. On your own Windows terminal, the existing `download` command also supports a `.csv` output with UTC times and broker spread: `python -m bot.cli download --symbol XAUUSDm --years 1 --allow-partial --out data/XAUUSDm_M1.csv`. Check actual coverage.
- `--start` inclusive, `--end` exclusive (UTC). Earlier input bars warm the signals; no positions or P&L are carried into a new replay window. Use a fresh output directory per comparison. `summary.json` includes reason counts, metrics, cost settings, Git revision and file hashes; `trades.csv` records one row per leg. Optional `decisions.jsonl` records every evaluated bar with the event time, touched price bands, decision, quote and rejection reason.
- Defaults: **no commission / no added slippage**, observed spread on ticks, ideal fills at touched stop and target (`--stop-fill barrier`). For a gap sensitivity use `--stop-fill first_quote` to fill stops at the first executable crossing quote. Real MT5 executions need not match either model. No simulated latency, swaps, margin rejections or L2 fill queue. All cash limits and reports assume a USD account.

## What is a zone decision?

1. Each enabled symbol has its own incremental engine. Form completed, UTC-aligned M1/M5/M15 candles without padding missing minutes. The scanner reacts after each M1 close; it does **not** predict an exact high/low on a still-forming candle. Two completed bars on each side confirm a pivot. Zone widths are fixed at formation, `max(tick_size, 0.15 × source ATR14)`. Nearby confirmed highs/lows cluster, with distinct pivot-touch counts. Buy-side liquidity is inferred above highs; sell-side below lows. These are **price proxies, not observed stop orders**.
2. If price touches a band and closes back out on its original side with directional momentum, classify a **respected pullback**. A penetration beyond the far edge followed by a reclaim is a **sweep/reclaim**. A weak candle, price still inside, or an unconfirmed higher-timeframe breach gets an explicit WAIT reason, not a market order.
3. A close beyond the band **on its source timeframe** accepts the break. A break of the latest confirmed swing is BOS. Flip support/resistance and wait for a *later* retest; never buy/sell the breakout candle solely because it broke. Invalidate a failed retest and expire stale ones. A lower low is not by itself a buy. Higher-timeframe/larger-touch-count levels can win priority; contradictory structure is skipped. HH/HL/LH/LL and projected rising-low/falling-high trendline proximity are recorded as context, not obligations to enter.
4. Momentum combines signed body/range, close location, and body/ATR (weights 0.40/0.35/0.25). Entry also needs a fresh quote near the band, room to the next *known* opposing zone/round-number objective, an affordable stop, and risk approval. A momentum-only continuation in the middle of the range is not an entry. Mark the confirmed setup consumed, not eligible again every poll. A fresh pivot can rearm it. Long data gaps reset stale structure; after a reset, 14 completed M15 bars are needed for full context.
5. Exits: `fixed_cash` is the requested control (no instant profit harvest). `next_zone` instead sets TP at the next known opposing objective if it affords **at least** the cash target, or closes on an opposite confirmed BOS with opposing candle momentum. SL is still attached; never widen it. Scheduled exits, the KILL file or end-of-replay can also end a position. Try `--exit-mode next_zone` offline. `--require-structural-stop` blocks trades when the cash SL cannot clear the adverse wick/far edge by one tick. The ledger also records structural stop price, dollar risk there and the indicative maximum lot that would fit; the bot **never resizes silently**.

## Configure symbols, lots and batches

Edit `zones.symbols` in `config.yaml` (the legacy `pilot` dictionary is **not** the active sizing interface). Only `XAUUSD` is enabled by default. `EURUSD` and `USDJPY` are disabled examples. Set `broker_symbol` to the exact Market Watch name, including suffix. Verify `point`, `tick_size`, `contract_size`, volume limits and currency with your symbol specification. USD-quoted instruments and USD-base FX with a USD deposit are supported; do not assume a contract for an arbitrary cross. The demo desk double-checks monetary limits using the broker's `order_calc_profit`.

| Setting | Meaning |
|---|---|
| `lots`, `tp_usd`, `sl_usd` | Per **leg** and per instrument. Gold defaults: **0.09 / $10 / $5**. Targets round to a valid price tick. |
| `legs` | Same-signal batch. One by default. Two gold legs = 0.18 lot, **$10** combined modeled risk, not $5. No martingale, pyramiding, fallback lots or top-ups. |
| `max_spread`, `assumed_spread`, `slippage` | Absolute **price units** of that symbol. Examples: 0.300 in gold is not 0.300 in EURUSD. Slippage and per-lot round-turn `commission` are zero for the experiment. |
| `zones.limits` | Account-wide open leg count, gross lots, aggregate modeled dollar risk, UTC daily loss block, persistent equity drawdown block, max age and strategy magic number. Limits are not guaranteed realized-loss bounds. |
| `rules.exit_mode`, `require_structural_stop` | Separate management and structural-compatibility switches. The primary cash experiment deliberately logs rather than rejects stop/structure mismatch. |

## Monitor / demo operation on Windows

1. Stop old `trade-all`/timeframe workers. Use one dedicated **USD hedging demo** account, a running MT5 terminal and one copy of this scanner per account. Verify broker hours (`run.market_close_timezone`, `pilot.daily_flatten_time`, `daily_reopen_time`, session/rollover configuration); the schedule is shared across the enabled list. Do not enable symbols whose hours/specs you have not checked.
2. `python -m pip install -e . MetaTrader5 tzdata` (Windows). `python -m bot.cli trade-all --config config.yaml` runs **observation only**. Both `trade` and `trade-all` use the same single scanner when zones are enabled. Do not launch three workers or pass `--timeframe`. Watch `reports/zone_runtime/<account-hash>/shadow.jsonl`. Historical bars on startup warm the engine, never generate a retroactive fill. Shadow logs are signal/price plans, not simulated account statements or broker acceptance.
3. **Only if deliberately testing demo orders**: change `run.mode` to `demo` and pass `--arm-demo` (or `--arm-orders`). Demo account type, broker identity, free margin, quote freshness, volume step, actual broker SL/TP distance, spread, before-send price/permissions, returns from MT5, and portfolio exposure are checked. `auto` and `live` cannot arm the zone desk. Legacy execution is shadow-only. This branch was tested with a simulated MT5 adapter, **not a connected broker**.
4. Armed runs log signals, accepted/rejected requests and actual deal events, including any real fee/swap/commission the terminal reports, to `demo.jsonl`. Paper assumptions do not erase broker charges. A durable intent per signal prevents re-sending an already attempted batch after restart. An unacknowledged/partial fill sets `manual_hold` in `state.json`; existing protected fills are not topped up. Stop the scanner, reconcile actual positions/deals and inspect the log before removing *only* the resolved hold with a backup. Do not delete state or reset a high-water halt to get more trades.
5. `KILL` in the working directory blocks entries and requests closure of **this scanner's** positions in the enabled whitelist when fresh quotes/permissions allow; it never authorizes closing manual tickets. Foreign positions/pending orders block new entries. Keep the current whitelist/magic until all owned positions are closed. Missing feed/terminal data can prevent software exits; broker SL/TP attachments remain essential. A changed account, failed lock, corrupt state or critical broker query error should be investigated, not bypassed. A real stop can fill through its level in a gap.

No automatic model training or deployment is implied. Risk and data checks can legitimately produce zero trades. Use replay evidence and broker demo logs before changing thresholds or frequency.
