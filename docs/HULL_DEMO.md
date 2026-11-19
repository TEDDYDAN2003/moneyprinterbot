# Hull labels → protected MT5 demo tickets

This is a **Python companion for MT5 Desktop on Windows**, not an `.ex5` file. It does not need a chart website, browser automation, API key or database. `trade`, `demo` and `trade-all` now route **only** to this scanner. One process monitors native **M1, M2, M5** for the selected instruments. No M15 orders or legacy zone/momentum fallback. [Standing startup instructions](../README.md#standing-instructions-for-the-desktop-agent) · [Copy just your profiles](Hull-demo-start.html) · [test evidence](../research/hull_labels/README.md).

## What triggers an entry?

**Version 2 defaults: `hull_len: 16`, `source: either`.** Each native timeframe keeps two independent A curves: `A(s) = WMA(2 × WMA(s,8) − WMA(s,16),4)` for `s = low` and `s = high`. One shared `B = WMA(3 × WMA(close,2) − WMA(close,4) − WMA(close,8),8)`. A strict upward B cross of **either** A is BUY; a strict downward cross of either A is SELL. Each previous value is compared to its **own** history. Equality on either close is not a cross. Two sources crossing the same way on one close = **one** label; sources crossing opposite ways on the same lane/close = **no** label, exit or entry from that lane. There is **no BUY/SELL alternation latch**. The older single-source mode is still explicit: `hull_len: 24`, `source: hl2`.

Only completed candles count (`copy_rates_from_pos` starts at index **1**). Warm-up/reconnect is never a trade. [Pine v6 raw-label companion](../indicators/FC-Hull-Either-16-v2.pine): paste into a new Pine Editor indicator and add to chart. A raw alert cannot know whether MT5 has open tickets; do not wire it directly to order submission. `closed_label` includes `a_low`, `a_high`, `cross_low` and `cross_high`; single-source `a` is intentionally null for Either. An observed source disagreement is logged separately as `source_cross_conflict`.

**First qualified cross while flat, not first cross forever in a region.** For one broker symbol, the first accepted label can start **one normal profile-sized batch**. If even one same-side ticket remains, a later repeat (including another timeframe) is consumed: no add, close, amendment or queued retest from that signal. SL/TP and the ladder continue to operate independently. Once every leg has closed, a **new** qualifying close may trade the same region and direction without a new CHoCH/BOS. Example: BUY at 10:00; one of ten BUYs still open at 10:02 BUY → skip; final leg closes at 10:02:01 → do **not** replay 10:02; fresh eligible BUY at 10:04 while flat → evaluate normally. A rejected first candidate does not permanently lock a region. Unresolved exposure, pending orders and reconciliation holds still block entries.

The scanner checks broker positions and, for a new batch, the short deal-history interval from the **signal close**, not just whether the account appears flat when polled. A same-side exit at or after that timestamp does not re-arm that cross (a timestamp tie is conservatively busy). On the dedicated account, a manual exit of the matching side/symbol counts too, even if its exit deal has a different magic. `same_side_batch_open`, `same_side_batch_at_signal_close` and `entry_deal_history_unavailable` are terminal entry decisions, not retry requests. An opposite exit is **not** a same-side repeat. Shadow reads actual demo exposure; it does not invent open tickets for shadow plans.

**Upgrade an existing checkout:** a private `config.demo.local.yaml` is a full config. To opt in, change only `hull.indicator.hull_len` to `16` and `hull.indicator.source` to `either`; keep other settings, positions and journal intact. After a safe pull, read-only preflight should show `signal_policy: fc_hull_either_v2`, `batch_entry_policy: first_eligible_while_flat_v1`, `indicator.hull_len: 16`, and the existing `new_ticket_exit_policy: profit_ladder_70_30_v1` when using the default ladder. Explicit non-default signal settings are reported for review, not silently overridden. Do not update over an armed scanner or open exposure. No automatic hot reload or migration of tickets.

A raw label needs the **[protected swing v2 entry filter](HULL_STRUCTURE.md)**. A fresh cross can qualify on the actual CHoCH close. A subsequent trend entry requires a completed counter-direction close after the leg tip and a signal strictly above the protected HL (BUY) or below the protected LH (SELL). The protected swing is not trailed to a minor pivot. No midpoint gate. The distinct post-BOS countertrend band remains. A signal wick is not the entry price: both its close and the next executable quote must qualify. Logs distinguish `label` from `entry_side` and report the reason for a rejection.

When `hull.indicator.exit_opposite` is `true` (the existing default), the terminal still requests closes of owned opposite-side tickets on the **original selected Hull label** before any replacement entry. That selection does not use the new entry filter. Startup labels are warm-up only, never orders or exits. The chart's raw labels are not the chart simulator's EMA-gated trades. The scanner keeps the optional `signal: ema_plan` prefilter but does not use the chart simulator's 10%-of-equity sizing, 1.5R target or graphics.

## How to read an entry timeline

- **Bar open is not signal close.** An M1 bar stamped 19:52 opens then; its signal can first be known at 19:53:00, in the same time basis. The logs use UTC. With the shipped limits, the last permitted check is 19:53:08, inclusive. A usable tick must satisfy `signal close ≤ tick time ≤ check time ≤ deadline` and be no more than 4 s old. Keep the terminal host clock synchronized; never shift timestamps to manufacture freshness.
- The scanner wakes around native closes and aims to poll every 250 ms during the entry window (MT5/OS latency can be longer). If its first quote is absent, from before close, or already too old, the selected entry can wait **in this running process only** while other symbols continue. On the first fresh quote it checks the actual BUY ask or SELL bid, the frozen structure band and the full plan. Outside the band, excessive spread, a funding or configured limit veto, busy symbol, malformed/future-dated quote or broker rejection are **not** reasons to try again. A late tick is never backdated. No retry after a send, no partial top-up, no replay on restart, no repeated opposite exit just because another quote was sampled.
- In `shadow.jsonl` / `demo.jsonl`, find `closed_label` for the symbol, timeframe and `signal_close_utc`. `reason: ready` refers to the raw signal; `entry_side` / `entry_reason` tell you if the closed bar qualified. For a selected entry, `entry_quote_check` gives `tick_utc`, raw broker `tick_time_msc`, `checked_at_utc`, age, deadline, bid/ask, and `stage`. `disposition: wait` is not a veto; `fresh_quote` confirms timing only. `label_decision.outcome` gives the final result: e.g. `shadow_plan_only`, `spread_cap`, `quote_outside_buy_below_bear_bos`, or `entry_window_expired`. At expiry, `tick_status` distinguishes a missing or pre-close quote from a valid but late check. An actual broker fill needs a demo deal, not just a `batch_preview`.

## Which candle supplies the stop?

The blue arrows in your screenshots indicate the preceding local pullback, not necessarily a textbook HL/LH. The default `local_pullback` turns that into a checkable rule:

1. On the **same signal timeframe**, take the last **up to seven completed candles before the signal candle**. If the preceding opposite Hull crossover from the selected signal stream is more recent, start there. The stop algorithm is unchanged; changing the signal stream can change which candles enter that window. This uses the prior Hull direction to identify the leg; it is not an EMA entry filter.
2. BUY → the **lowest low** in that window. SELL → the **highest high**. Newest wins a price tie. No future candles, global high/low or confirmation by three *later* bars. A screenshot cannot uniquely specify a lookback; `hull.indicator.pullback_bars` changes it explicitly.
3. Freeze that wick at the decision. Use the script's Wilder ATR14 buffer: `buy SL = low − 0.1 × ATR`, `sell SL = high + 0.1 × ATR`; optional extra pips default to zero. Round outward to a legal price tick. Amount `atr_buffer: 0` puts the stop on the wick (outward tick rounding still applies). There is **no $5 stop or fixed per-position dollar limit**. Never widen the stop after entry or squeeze it toward the entry to pass a risk check.
4. `confirmed_pivot` is available if you want the *original chart simulator's* alternative: latest 3-left/3-right confirmed pivot with newer equal extremes preferred. This is **not** the screenshot-driven local-pullback default.

The order SL is the computed chart price, **not** that price plus an unreported spread allowance. Longs exit on bid; shorts exit on ask. A spread expansion can stop a short even if the bid chart doesn't revisit its high. If SL is already breached or too close for the broker, skip. The bot still prices the loss at that stop even when no portfolio dollar cap is set.

## Profit ladder for new tickets

The profile’s USD number is the **original, per-ticket gross profit target**, not a batch target. `hull.exit_policy` defaults to `profit_ladder_70_30_v1`, including when an older local YAML omits the field. Every new position starts with its own hard swing SL and 100% TP. After a verified fill, the scanner records that ticket’s actual side, volume, entry and original target. It does not adopt existing positions. Previously journaled fixed-target tickets keep their original cash-exit behavior; a later config/profile change does not change a ticket’s policy or base.

| Executable gross profit vs. original goal | New SL profit level | New broker TP | Example with a $10 original goal: trigger → SL / TP |
|---:|---:|---:|---|
| 70% | 60% | 120% | +$7 → +$6 / +$12 |
| 100% | 90% | 150% | +$10 → +$9 / +$15 |
| 130% | 120% | 180% | +$13 → +$12 / +$18 |
| 160% | 150% | 210% | +$16 → +$15 / +$21 |

From there add **30 percentage points to all three columns** at each step. The base never compounds. For example, if +$7 was observed and the broker confirmed the first move, +$10 advances the next rung; it is **not** a request to close at the old $10 target. A reversal to +$9 after that move can hit the raised stop. Nothing is locked before the first amendment is actually accepted.

- **No candle-close or extra trend vote.** Check each ticket’s executable **bid for a BUY, ask for a SELL**. Convert price to modeled USD using the broker’s `order_calc_profit` at the actual fill and volume; use the tick grid to price both new levels. Ten tickets retain ten independent exits. While enrolled tickets are open the single Windows scanner aims to sample every 250 ms and can check between batch legs. It does not see every tick: MT5 latency, other broker calls, sleep or disconnect can delay/miss a step.
- **One request, both levels.** Never remove/widen SL or move TP closer. If the new pair violates broker stop/freeze distances or MT5 refuses it, the last confirmed levels stay in effect. Reconsider only on fresh, still-eligible quotes. The gap between trigger and stop is just **$1 gross** on a $10 ticket; on some lots/symbols that is less than a legal stop distance. No substitute lower floor or bare TP extension. There is no Hull send-count cap. A definite broker refusal, including “too many requests,” still uses the existing per-ticket backoff; an uncertain acknowledgement must be reconciled, not resent.
- **Server exits come first.** Price may hit the current TP or SL before an amendment; if the scanner observes a crossed hard barrier it leaves that pair alone, even on a later retreat. The configured opposite Hull exit, KILL and missing-protection exits still apply. The last broker SL/TP remain attached if the scanner stops, but they cannot advance offline. Stops can slip through gaps; fees, spread and changing conversion affect realized net. This does not guarantee profit or an exit exclusively at SL.
- **Audit and recovery.** Preflight and `started.summary` show `new_ticket_exit_policy` and each symbol’s `ladder_preview_per_ticket` in USD. The observer is read-only: it does not amend tickets. Armed logs distinguish `profit_ladder_enrolled`, `profit_ladder_step` (both prices verified on the broker ticket), `profit_ladder_deferred`, and `profit_ladder_broker_barrier` (observed touch, **not** a confirmed fill). The journal stores original bases, confirmed pairs and pending requests in `ladder_by_ticket`. A lost reply/crash never triggers a blind resend. Both broker levels must match the pending pair to confirm; unresolved requests or unexpected external edits block new entries for review. Do not delete the journal to clear a hold.

`fixed_tp` is available explicitly for comparisons/new fixed-target tickets. It is **not** the default. The ladder calculation is independent of account type; the shipped order bridge still refuses real-money accounts. This is not a live-account rollout.

## Rate and symbols

| Rate | Requested count per eligible label | At 0.09 lot and $10 **initial** target each |
|---|---:|---|
| Slow | 10 | 0.90 lot / $100 gross target |
| Medium | 4 if it fits, else 3 | 0.27–0.36 lot / $30–$40 |
| Fast | 7 if it fits, else 6 | 0.54–0.63 lot / $60–$70 |

If even the minimum count cannot fit, **skip the whole signal**. No smaller surprise order. MT5 sends are sequential, not atomic; if a later leg fails, previously filled protected tickets remain and the bot does not retry or top up the batch. One active batch per instrument; a new batch cannot start while the symbol remains occupied. The configured opposite-label exit is evaluated separately. For simultaneous **entry-qualified** labels on an instrument: same direction → use M5, then M2, then M1 (single batch); conflicting directions → skip, never hedge a contradiction. Different selected instruments may trade simultaneously subject to account limits. Rate changes batch size, not signal sensitivity or poll speed.

Default: only **XAUUSD** selected. Configured keys also include **EURUSD, AUDUSD, GBPJPY, GBPUSD, USDJPY, NAS100USD, US30, 1HZ100V**. On the connected Exness terminal, AUDUSD and GBPJPY resolve to `AUDUSDm` and `GBPJPYm`. Their Hull profiles use 1 lot per ticket, the EURUSD $10 target and slow rate; the GBPJPY spread cap is symbol-specific. FX/metals/CFDs/synthetics can have very different lot steps, trading hours and cash values. Resolver requires exactly one matching configured alias in the terminal catalogue; otherwise set `hull.symbols.<KEY>.broker_symbol` to one exact Market Watch name. No substring guessing or automatic trading of everything in Market Watch. To use another instrument, explicitly add a profile with its exact name, lot, target, rate, `pip_size`, appropriate spread cap and session. `extra_pips: 0` means pip size does not affect the default SL. A template 0.01 lot is not necessarily valid: check specifications rather than silently rounding size up. Live calculations use broker tick size, digits, stop/freeze distance, volume limits, margin and account-currency conversion. One safe process can accept different profiles:

```powershell
python -m bot.cli hull-preflight --profile XAUUSD:0.09:10:fast --profile EURUSD:0.01:8:slow
python -m bot.cli trade-all --profile XAUUSD:0.09:10:fast --profile EURUSD:0.01:8:slow
```

Or apply one set of overrides: `--symbols EURUSD GBPUSD --lots 0.01 --target-usd 10 --rate medium`. Do not mix these with `--profile`. Omit `--timeframe` to run all three. Use `--timeframe M2` only for a single-lane comparison.

## Hull portfolio limits and broker funding

The default applies to every selected Hull profile. There is **no bot-imposed ceiling** on open ticket count, gross lots or modeled USD loss at stops, and **no extra percentage of equity** is set aside as a margin reserve. Lots, batch counts, chart stops, per-ticket targets and the profit ladder are unchanged. For example, an otherwise eligible Gold batch of 10 × 0.10 lots with $204.87 modeled risk per ticket records **$2,048.70** in `batch_preview.risk_batch` rather than rejecting it against the old $1,000 cap. This is potential loss, not required margin; actual loss can be larger.

**No Hull drawdown halt or per-minute send limit.** A drop of $75 (or more) below a previous equity peak no longer blocks the next funded entry or the rest of an eligible batch. Entry requests and per-ticket ladder amendments no longer compete for 40 slots per minute. Eight eligible slow profiles could request 80 entries in a minute, plus ladder updates; MT5 can still throttle or reject them. Orders are sequential, require valid positive equity and are never guaranteed to fill. Losses can accumulate without an automatic drawdown stop.

**Broker funding is still mandatory.** Before entry (and again for the remaining tickets), the bot subtracts the full requested count × MT5’s single-ticket margin estimate from current free margin. A funded batch can leave less than 60% of equity free, even zero; an unfunded batch cannot proceed. In armed demo mode each ticket also needs an accepted MT5 `order_check` with valid projected free margin. Only a confirmed broker result establishes a fill. The offline replay does not simulate broker margin or approvals.

| New log reason | What to check |
|---|---|
| `insufficient_free_margin` | Free margin is negative now or would be negative after funding. |
| `margin_floor` | Funding fits, but free margin would be below an **explicit positive** Hull reserve in this config. This reason cannot occur with the shipped 0% reserve. |
| `no_margin_estimate` | Required margin data were missing or invalid. No send. |
| `broker_precheck_rejected` | MT5 returned no check result or rejected the request; see the recorded `retcode` when available. |

For the first three reasons, `margin_veto` identifies the stage (`first_plan`, `next_leg`, `pre_send` or `order_check`), number of legs checked, equity, current/projected free margin and required reserve in USD. An unknown value is `null`. A batch preview is **not** a promise of a fill.

**Updating an existing Windows demo config:** `git pull` does not edit the ignored `config.demo.local.yaml`. Positive values for **the four optional keys below** still take effect. To adopt their defaults, edit just these four keys **inside the existing `hull.limits` mapping** (or remove just those keys to inherit their code defaults). Do not add a duplicate mapping, replace the file or edit the separate top-level `risk` / `zones` settings.

```yaml
# Within hull.limits only; keep its other settings.
max_positions: null
max_total_lots: null
max_open_risk_usd: null
min_free_margin_pct: 0
```

Use read-only `hull-preflight --config config.demo.local.yaml` **with the same profiles** before a new demo start. Verify the three `null` values and `0` in `limits`; `review_before_demo` lists any of these four limits that are enabled. Review the account, exposure and other warnings as usual. Preflight and `started.summary.retired_limits` list `drawdown_usd` and `max_orders_minute`; neither is active even if an older local Hull YAML explicitly sets it. Old journal `guard` and `sends` data have no effect. Leave the journal intact: consumed signals, manual holds and pending ladder requests still matter. A running scanner does not reload these changes, and an update does not replay past setups.

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
3. For a **new** local demo config, copy `config.yaml` to the ignored `config.demo.local.yaml` and set `run.mode: demo` in the copy. If one already exists, **do not overwrite it**; review the [four Hull defaults](#hull-portfolio-limits-and-broker-funding). Re-run read-only preflight on the local file with the same profiles and review the report. Only after separate explicit arming approval and verification of demo-terminal permissions, launch **one** process:
   ```powershell
   python -m bot.cli trade-all --config config.demo.local.yaml --profile XAUUSD:0.09:10:slow --arm-demo
   ```
   Paper is the shipped default. No `--arm-demo` = no sends. A real, contest, non-USD, netting, disconnected or changed account cannot arm. All examples are operator instructions: the development sandbox did not connect to a terminal or place orders.
4. Check the **effective** limits, not just the checked-in YAML. Hull has no automatic peak-to-current or daily loss stop (`daily_loss_usd` is **not** enforced). Do not resize tickets, move the SL or change an operator-selected limit to force an entry.
5. Logs: `reports/hull_runtime/<account-hash>/{shadow,demo}.jsonl`; recorded broker deals separate profit, fee, commission and swap. Durable intent, per-ticket goals, ladder confirmations and manual hold live in `~/.moneyprinterbot/hull/<account-hash>/state.json`. An OS lock prevents two armed Hull scanners under the same Windows user from trading the same account. Foreign positions or pending orders block new batches. A rejected/uncertain/partial send may need manual reconciliation; **do not delete state to retry**. Check actual tickets/deals, full size and protection, preserve a backup, then remove only a resolved `manual_hold` while stopped. Keep symbol whitelist/magic unchanged while tickets are open.
6. Ctrl+C stops the software; broker SL/TP remain attached. The `KILL` file in the **bot's working directory** blocks new orders and requests closure of owned, allowlisted positions while the scanner is running and quotes/permissions permit. No ordinary session or momentum exit; configured session/rollover gates block new entries only. Review market-specific hours, especially CFDs/synthetics. Software exits cannot work while disconnected.
