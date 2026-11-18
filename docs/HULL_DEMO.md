# Hull labels → protected MT5 demo tickets

This is a **Python companion for MT5 Desktop on Windows**, not an `.ex5` file. It does not need a chart website, browser automation, API key or database. `trade`, `demo` and `trade-all` now route **only** to this scanner. One process monitors native **M1, M2, M5** for the selected instruments. No M15 orders or legacy zone/momentum fallback. [Standing startup instructions](../README.md#standing-instructions-for-the-desktop-agent) · [Copy just your profiles](Hull-demo-start.html) · [test evidence](../research/hull_labels/README.md).

## What triggers an entry?

With your supplied defaults, `a = WMA(2 × WMA(hl2,12) − WMA(hl2,24),5)` and `b = WMA(3 × WMA(close,4) − WMA(close,6) − WMA(close,12),12)`. BUY when `b > a` now **and** `b < a` on the previous close; SELL for the reverse. An equal value is not a crossover. Missing WMA windows do not become zeros. Look only at completed candles (`copy_rates_from_pos` starts at index **1**, not live index 0). Startup / long-gap history is warm-up only. The first fresh observed quote at or after the signal close is eligible, not a retrospective fill at the label or candle wick.

A raw label now needs the **[internal structure entry filter](HULL_STRUCTURE.md)**. Each native lane independently checks confirmed CHoCH → HH → HL (bearish mirror: LL → LH), the 50% pullback band, or the separate post-continuation-BOS countertrend band. A signal wick is not the entry price: both its close and the next executable quote must qualify. Logs distinguish `label` from `entry_side` and report the reason for a rejection.

When `hull.indicator.exit_opposite` is `true` (the existing default), the terminal still requests closes of owned opposite-side tickets on the **original selected Hull label** before any replacement entry. That selection does not use the new entry filter. Startup labels are warm-up only, never orders or exits. The chart's raw labels are not the chart simulator's EMA-gated trades. The scanner keeps the optional `signal: ema_plan` prefilter but does not use the chart simulator's 10%-of-equity sizing, 1.5R target or graphics.

## How to read an entry timeline

- **Bar open is not signal close.** An M1 bar stamped 19:52 opens then; its signal can first be known at 19:53:00, in the same time basis. The logs use UTC. With the shipped limits, the last permitted check is 19:53:08, inclusive. A usable tick must satisfy `signal close ≤ tick time ≤ check time ≤ deadline` and be no more than 4 s old. Keep the terminal host clock synchronized; never shift timestamps to manufacture freshness.
- The scanner wakes around native closes and aims to poll every 250 ms during the entry window (MT5/OS latency can be longer). If its first quote is absent, from before close, or already too old, the selected entry can wait **in this running process only** while other symbols continue. On the first fresh quote it checks the actual BUY ask or SELL bid, the frozen structure band and the full plan. Outside the band, excessive spread, risk, busy symbol, malformed/future-dated quote or broker rejection are **not** reasons to try again. A late tick is never backdated. No retry after a send, no partial top-up, no replay on restart, no repeated opposite exit just because another quote was sampled.
- In `shadow.jsonl` / `demo.jsonl`, find `closed_label` for the symbol, timeframe and `signal_close_utc`. `reason: ready` refers to the raw signal; `entry_side` / `entry_reason` tell you if the closed bar qualified. For a selected entry, `entry_quote_check` gives `tick_utc`, raw broker `tick_time_msc`, `checked_at_utc`, age, deadline, bid/ask, and `stage`. `disposition: wait` is not a veto; `fresh_quote` confirms timing only. `label_decision.outcome` gives the final result: e.g. `shadow_plan_only`, `spread_cap`, `quote_outside_buy_below_bear_bos`, or `entry_window_expired`. At expiry, `tick_status` distinguishes a missing or pre-close quote from a valid but late check. An actual broker fill needs a demo deal, not just a `batch_preview`.

## Which candle supplies the stop?

The blue arrows in your screenshots indicate the preceding local pullback, not necessarily a textbook HL/LH. The default `local_pullback` turns that into a checkable rule:

1. On the **same signal timeframe**, take the last **up to seven completed candles before the signal candle**. If the preceding opposite Hull crossover is more recent, start there. This uses the prior Hull direction to identify the leg; it is not an EMA entry filter.
2. BUY → the **lowest low** in that window. SELL → the **highest high**. Newest wins a price tie. No future candles, global high/low or confirmation by three *later* bars. A screenshot cannot uniquely specify a lookback; `hull.indicator.pullback_bars` changes it explicitly.
3. Freeze that wick at the decision. Use the script's Wilder ATR14 buffer: `buy SL = low − 0.1 × ATR`, `sell SL = high + 0.1 × ATR`; optional extra pips default to zero. Round outward to a legal price tick. Amount `atr_buffer: 0` puts the stop on the wick (outward tick rounding still applies). There is **no $5 stop or fixed per-position dollar limit**. Never widen the stop after entry or squeeze it toward the entry to pass a risk check.
4. `confirmed_pivot` is available if you want the *original chart simulator's* alternative: latest 3-left/3-right confirmed pivot with newer equal extremes preferred. This is **not** the screenshot-driven local-pullback default.

The order SL is the computed chart price, **not** that price plus an unreported spread allowance. Longs exit on bid; shorts exit on ask. A spread expansion can stop a short even if the bid chart doesn't revisit its high. If SL is already breached, too close for the broker, or incompatible with the portfolio budget, skip.

## Target, rate and symbols

The lot and target are **per position**. Each ticket is submitted with its own hard SL and TP; calculate the nearest favorable tradable tick meeting the USD target using the broker’s `order_calc_profit`, not an assumed pip value. A changed fill is checked and TP is aligned to the actual open if necessary, without widening SL. The supervisor requests a close if the broker still reports an owned ticket at/above its USD profit goal. No trailing, giveback or momentum hold. The configured opposite crossover exit is separate. Exact realized net can differ due to rounding, gaps, conversion, broker fees or execution. KILL and missing protection are emergency exceptions.

| Rate | Requested count per eligible label | At 0.09 lot and $10 each |
|---|---:|---|
| Slow | 10 | 0.90 lot / $100 gross target |
| Medium | 4 if it fits, else 3 | 0.27–0.36 lot / $30–$40 |
| Fast | 7 if it fits, else 6 | 0.54–0.63 lot / $60–$70 |

If even the minimum count cannot fit, **skip the whole signal**. No smaller surprise order. MT5 sends are sequential, not atomic; if a later leg fails, previously filled protected tickets remain and the bot does not retry or top up the batch. One active batch per instrument; a new batch cannot start while the symbol remains occupied. The configured opposite-label exit is evaluated separately. For simultaneous **entry-qualified** labels on an instrument: same direction → use M5, then M2, then M1 (single batch); conflicting directions → skip, never hedge a contradiction. Different selected instruments may trade simultaneously subject to account limits. Rate changes batch size, not signal sensitivity or poll speed.

Default: only **XAUUSD** selected. Configured keys also include **EURUSD, GBPUSD, USDJPY, NAS100USD, US30, 1HZ100V**. FX/metals/CFDs/synthetics can have very different lot steps, trading hours and cash values. Resolver requires exactly one matching configured alias in the terminal catalogue; otherwise set `hull.symbols.<KEY>.broker_symbol` to one exact Market Watch name. No substring guessing or automatic trading of everything in Market Watch. To use another instrument, explicitly add a profile with its exact name, lot, target, rate, `pip_size`, appropriate spread cap and session. `extra_pips: 0` means pip size does not affect the default SL. A template 0.01 lot is not necessarily valid: check specifications rather than silently rounding size up. Live calculations use broker tick size, digits, stop/freeze distance, volume limits, margin and account-currency conversion. One safe process can accept different profiles:

```powershell
python -m bot.cli hull-preflight --profile XAUUSD:0.09:10:fast --profile EURUSD:0.01:8:slow
python -m bot.cli trade-all --profile XAUUSD:0.09:10:fast --profile EURUSD:0.01:8:slow
```

Or apply one set of overrides: `--symbols EURUSD GBPUSD --lots 0.01 --target-usd 10 --rate medium`. Do not mix these with `--profile`. Omit `--timeframe` to run all three. Use `--timeframe M2` only for a single-lane comparison.

## Windows demo checklist

1. Install 64-bit Windows Python 3.11+ supported by the MT5 package. Start MT5 and log in locally to a dedicated **USD hedging DEMO** account. Identify running scanners and exposure first. Never interrupt an armed/unknown process, modify its checkout or touch manual positions; stop and ask. Follow the [checkout/version check](../README.md#check-the-checkout-before-reusing-an-observer). Don't change permissions on a real account. In the project root:
   ```powershell
   python -m pip install -e ".[test]" MetaTrader5 tzdata
   python -m pytest -q
   python -m bot.cli validate-config
   python -m bot.cli hull-preflight --profile XAUUSD:0.09:10:slow
   ```
   The preflight reads symbol specs, permissions, existing exposure, and the most recent historical label/anchor for each native timeframe. These historical labels are **never submitted**. Investigate wrong/ambiguous names, unavailable history, stale/wide spreads and invalid volume rather than bypassing guards. If auto-detection picks the wrong terminal, set `mt5.terminal_path` in your local config.
2. `python -m bot.cli trade-all --profile XAUUSD:0.09:10:slow` is a **read-only observer**, not a shadow P&L backtest. Compare `closed_label`, `entry_side`, `entry_reason`, `anchor_s`, `raw_sl` and `batch_preview` with closed candles on **your broker feed**. That need not be the OANDA feed pictured. A risk veto logs per-leg loss and the minimum batch's loss separately. `--max-iterations` allows a bounded smoke run; a one-iteration run only warms history.
3. Only on explicit approval, copy `config.yaml` to the ignored `config.demo.local.yaml`, set **only** `run.mode: demo` in that copy, verify Algo Trading / external Python permissions on the demo terminal, and launch **one** process:
   ```powershell
   python -m bot.cli trade-all --config config.demo.local.yaml --profile XAUUSD:0.09:10:slow --arm-demo
   ```
   Paper is the shipped default. No `--arm-demo` = no sends. A real, contest, non-USD, netting, disconnected or changed account cannot arm. All examples are operator instructions: the development sandbox did not connect to a terminal or place orders.
4. The checked-in demo limits (from the scaled profiles, **not changed by the quote-timing fix**) are **60 open tickets, 54 gross lots, $1,000 modeled open risk**, $75 peak-equity drawdown halt, at least 60% free margin and 40 sends/minute. The current Hull guard is drawdown-based; it does **not** enforce a separate daily loss stop (`daily_loss_usd` ships as 0). Do not rely on that field for a daily stop. Read the effective preflight limits and verify the **whole batch** fits; there is no fixed $5 per-ticket stop. Actual losses can exceed modeled risk. Do not raise a limit, resize a ticket or move SL to force an entry.
5. Logs: `reports/hull_runtime/<account-hash>/{shadow,demo}.jsonl`; recorded broker deals separate profit, fee, commission and swap. Durable intent, per-ticket goals, equity high-water and manual hold live in `~/.moneyprinterbot/hull/<account-hash>/state.json`. An OS lock prevents two armed Hull scanners under the same Windows user from trading the same account. Foreign positions or pending orders block new batches. A rejected/uncertain/partial send may need manual reconciliation; **do not delete state to retry**. Check actual tickets/deals, full size and protection, preserve a backup, then remove only a resolved `manual_hold` while stopped. Keep symbol whitelist/magic unchanged while tickets are open.
6. Ctrl+C stops the software; broker SL/TP remain attached. The `KILL` file in the **bot's working directory** blocks new orders and requests closure of owned, allowlisted positions while the scanner is running and quotes/permissions permit. No ordinary session or momentum exit; configured session/rollover gates block new entries only. Review market-specific hours, especially CFDs/synthetics. Software exits cannot work while disconnected.
