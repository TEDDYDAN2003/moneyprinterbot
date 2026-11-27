# Dynamic Hull → protected MT5 demo tickets

**Entry route:** market orders remain the default. For paired candle-relative BUY/SELL limits, use the [direct retest guide](HULL_RETEST.md). Changes and tests do not authorize arming.

**Active Python desk:** [intrabar market entries, trigger swing stops and private-YAML timing](HULL_INTRABAR.md). M5 → M3, M10 → M5, M15 → M10. No M1 trigger; M30/M45 are historical/study frames, not active parents. [Previous M1 release](HULL_M1.md).

`trade`, `demo` and `trade-all` run the Python companion for Windows MT5 Desktop, not an Expert Advisor. No webhook or database is required. The template selects M5/M10/M15 as parents; M3/M5/M10 supply their respective triggers. The descriptions of historical parent-close trades are not an extra source of entries. Install only when flat, with no unresolved exposure or running manager to interrupt.

## What triggers an entry?

The shipped paired market clock uses the [intrabar rules](HULL_INTRABAR.md): a fresh trigger cross, both confirmed structures aligned and a confirmed trigger-range HL/LH. Opposite and originating-cross invalidation are independent exits. Market hours, spread, quote freshness, risk, funding and broker checks still apply.

## Historical / explicit confirmed-close timing and stop

The next two sections describe `confirmed_cross_next_bar`, **not** the shipped intrabar market route. Use them to interpret closed and retest receipts. For `q…` market receipts see [intrabar audit](HULL_INTRABAR.md#audit-and-limits).

**Entries: close → next candle.** `closed_label` records the completed native cross; `trigger_msc` is its close, not the fill. For example, an M15-owned BUY on the 10:00–10:10 M10 candle can be submitted on the first usable quote observed at or after 10:10:00 in the immediate successor candle. The executable BUY opens at ask, SELL at bid. This is **not** a guaranteed opening price, and it does **not** wait for the *next* candle to close. The shipped timing budget is 8 seconds after signal close and 4 seconds for quote age. A missing/pre-close/stale first quote may wait briefly within that original deadline; an out-of-window, unsafe or expired opportunity is not carried forward.

`pair_trigger_exit_signal` records a verified trigger cross as `phase: live` or `closed`, with its owning parent and trigger. A live cross is exit-only. It can close opposing owned tickets but never open an order, reserve the next candle, spend the MSS skip or suppress a separately confirmed close. A cross that disappears by the close produces **no entry**. An MSS still flattens its own lane. Inspect `label_decision.outcome`; an exit request or an entry setup is not a broker fill.

## Which candle supplies the closed-mode stop?

**Use the completed candle on which the Hull crossed, on the same symbol and timeframe.** Never use the entry candle's developing wick, the earlier seven-bar pullback or a previous swing. For historical parent-close M45 trades the wick is the extreme of **all three** consecutive M15 components, not just the third. Each new ticket carries the initial SL and TP in the opening request.

| Signal | Wick on the broker’s bid chart | Raw chart stop (before SELL translation) |
|---|---|---|
| BUY | Cross candle **low** | Low minus buffer, round **down** to a legal trade tick |
| SELL | Cross candle **high** | High plus buffer, round **up** to a legal trade tick |

`buffer = max(trade_tick_size, atr_buffer × Wilder ATR at the cross close + extra_pips × pip_size)`. Defaults: ATR14, `atr_buffer: 0.1`, `extra_pips: 0`. Even a zero ATR buffer leaves at least **one full broker trade tick** outside the wick. A tick is not necessarily one point or one pip. Example: BUY wick 2000.37, ATR 0.80, trade tick 0.05 → buffer 0.08 → 2000.29 rounded **down to 2000.25**. This is a price example, not a trade recommendation.

The **raw chart stop** is frozen at confirmation for every leg and any unsent reprice. A BUY stop is checked against executable bid; a SELL stop against ask. For Bid chart candles, BUY SL stays at the raw level because the broker also triggers it on Bid. A new SELL SL adds the current Ask-Bid spread to the raw candle stop and rounds up to the broker trade tick because its broker trigger is Ask. Cash-risk and funding checks use that final buffered price. An eligible unsent market request can be rebuilt on a narrower spread inside the existing time/drift budget; it must pass all checks again. Never send an under-buffered SELL on either route. An accepted limit keeps its placement bracket, not a guarantee of the spread at activation. If the price gaps through the stop, it is too close for the broker, the trade grid changes, or another order check fails, skip the unsent trade/legs. Do not pull the stop in, push it out, substitute a cash or swing stop, or top up a partial batch. An accepted profit-ladder move may later *tighten* the stop as before. No open tickets are re-anchored on upgrade.

Preflight: `entry_timing: confirmed_cross_next_bar`, `entry_quote_policy: first_fresh_post_close_tick_v1`, `new_ticket_stop_policy: confirmed_pair_trigger_candle_wick_v1`, `stop_spread_policy: sell_ask_entry_only_exact_risk_v2`. Compare `closed_label.anchor_s` / `cross_candle_open_s`, `cross_candle_wick`, `atr`, `stop_tick_size` and raw `frozen_cross_sl` against final `batch_preview.sl`, `stop_spread_buffer_price`, `risk_batch` and the actual broker request. A large signal candle can still mean large loss exposure. This change neither enables a dollar cap nor guarantees lower realized losses; check existing `hull.limits.max_open_risk_usd`, spread, batch size, slippage and fees in demo.

## Profit ladder for new tickets

**Initial goal per new ticket = configured `target_usd` + that ticket’s entry spread in USD.** On the quote used for its order, the scanner asks MT5 for the immediate loss at that symbol and volume: BUY opens at ask and marks to bid; SELL opens at bid and marks to ask. A displayed spread of “13” points does **not** mean $13. A zero spread adds nothing. If the spread exceeds the existing price-unit cap or MT5 cannot value it, the scanner skips rather than assuming zero. It does not wait for a narrower spread.

For a **$10 base + $13 cash spread**, the goal is **+$23 gross**, not +$10 net. The broker TP is the first tradable price reaching that goal from the fill. Price-grid rounding can put the modeled profit slightly above $23. The spread is already reflected in executable P/L: do not deduct the $13 again from broker profit. Commission, swap, fees, slippage and changes in currency conversion can change the realized result. A larger goal can go unfilled.

Each leg gets its own goal, even within one batch. It is frozen at send: if the fill slips, re-anchor the TP to the verified fill, not to a newly sampled spread. During that initial TP alignment the SL stays at its original verified broker price. With the default `profit_ladder_70_30_v1`, all rungs use **that frozen goal**, not the rounded TP profit or the latest spread. Older and already-open tickets are not migrated or adopted; a restart or profile change does not reset their recorded goals.

**Half-risk stop on a return to entry (new ladder tickets).** Break-even means this ticket’s actual MT5 `price_open`; **it is a trigger, not a stop at entry**. After a verified market or retest fill, the scanner saves its actual fill and original broker SL. No signal price, pending limit, batch average, fee buffer or second spread charge is used.

| Ticket | First fresh quote strictly in profit | Later fresh quote comes back | Stop requested if legal |
|---|---|---|---|
| BUY at 100, original SL 90 | Bid above 100 → remember it; leave SL 90 | Bid at or below 100 | **95**, same TP |
| SELL at 100, raw Bid-chart SL 110, spread 2 → broker SL 112 | Ask below 100 → remember it; leave SL 112 | Ask at or above 100 | **106**, same TP |

The return quote must have a later broker timestamp than the first profitable observation. A touch coming **from loss alone** does nothing. Each ticket has its own evidence; a chart wick, last price or displayed P/L is not proof. The midpoint is `(actual_fill + original_SL) / 2`, rounded toward entry on the trade-tick grid (up for BUY, down for SELL). The midpoint uses two broker prices; **do not add spread again**. It limits modeled price distance, not realized cash loss after fees, gaps or slippage.

The broker keeps the original bracket until a checked, ticket-specific amendment succeeds. An amendment is rechecked on a fresh executable quote; a wider spread is never a reason to loosen an installed broker stop. If price jumps past the midpoint, the broker freezes modifications, the quote rebounds into profit during the check, or the unchanged TP is too close, the scanner does **not** force a market exit or install a different stop. It can retry on a later eligible quote while the original bracket is still active. Gaps, polling delays, fees and slippage matter. A touched broker SL/TP or an existing exit takes priority.

The halfway stop is the **final pre-ladder stop**. It does not trail on repeated touches, move to entry on the rebound, or count as rung 1. If a stronger normal rung is executable first or later, take that rung directly. Once set, a stronger stop is never moved back to the midpoint. The normal trigger/lock/TP table and its frozen base are unchanged.

`hull.pre_ladder_breakeven` defaults to `true` even if absent in private YAML. `false` opts **future** tickets out. Preflight / `started.summary` shows `new_ticket_breakeven_policy: actual_fill_pullback_half_initial_risk_v1` (or `off`) and `stop_spread_policy: sell_ask_entry_only_exact_risk_v2`. `profit_ladder_pullback_profit_seen` is an observation, **not** an amendment. `profit_ladder_pullback_half_confirmed` records the ticket, entry, original SL, profitable observation, return quote and verified pair after read-back. Neither event is an exit receipt. Existing tickets tagged `actual_fill_first_legal_quote_v1` retain their earlier hard-break-even policy, including pending read-backs. Untagged, manual and fixed-TP tickets are not retrofitted. A pending order alone is not a filled ticket. The spread generation is also frozen per ticket. Records from before the spread update keep their unbuffered amendment rules. Records already using the interim live-spread policy keep that behavior (their SELL lock can be below the nominal rung). An outstanding amendment is read back exactly as saved, not recalculated or replayed.

| Executable gross profit vs. original goal | New SL profit level | New broker TP | Example with a $23 frozen goal: trigger → SL / TP |
|---:|---:|---:|---|
| 70% | 60% | 120% | +$16.10 → +$13.80 / +$27.60 |
| 100% | 90% | 150% | +$23.00 → +$20.70 / +$34.50 |
| 130% | 120% | 180% | +$29.90 → +$27.60 / +$41.40 |
| 160% | 150% | 210% | +$36.80 → +$34.50 / +$48.30 |

For example, with a $10 frozen goal and $1 per price unit, the SELL at 100 reaches +$7 at Ask 93. Its first ordinary rung requests **SL 94** (+$6 gross) and **TP 88** (+$12 gross). These are executable exit prices already. Both cash floors are checked again before sending.

From there add **30 percentage points to all three columns** at each step. The base never compounds. In this example each step adds $6.90. If the broker confirmed the first move, +$23 advances the next rung; it is **not** a request to close at the original TP. A reversal to +$20.70 after that move can hit the raised stop. Nothing is locked before the first amendment is actually accepted.

- **No candle-close or extra trend vote.** Check each ticket’s executable **bid for a BUY, ask for a SELL**. Convert price to modeled USD using the broker’s `order_calc_profit` at the actual fill and volume; use the tick grid to price both new levels. Ten tickets retain ten independent exits. While enrolled tickets are open the single Windows scanner aims to sample every 250 ms when the supervisor is available. It does not see every tick: batch sends, MT5 latency, other broker calls, sleep or disconnect can delay/miss a step.
- **One request, both levels.** Never remove/widen SL or move TP closer. If the new pair violates broker stop/freeze distances or MT5 refuses it, the last confirmed levels stay in effect. Reconsider only on fresh, still-eligible quotes. The gap between trigger and stop is **10% of the frozen goal** ($2.30 on a $23 goal); on some lots/symbols that is less than a legal stop distance. No substitute percentage lock or bare TP extension; the separate pullback stop above must satisfy its own checks. There is no Hull send-count cap. A definite broker refusal, including “too many requests,” still uses the existing per-ticket backoff; an uncertain acknowledgement must be reconciled, not resent.
- **Server exits come first.** Price may hit the current TP or SL before an amendment; if the scanner observes a crossed hard barrier it leaves that pair alone, even on a later retreat. The lane-specific MSS and raw opposite Hull exits, KILL and missing-protection exits still apply. The last broker SL/TP remain attached if the scanner stops, but they cannot advance offline. Stops can slip through gaps; fees, spread and changing conversion affect realized net. This does not guarantee profit or an exit exclusively at SL.
- **Audit and recovery.** Preflight and `started.summary` show `new_ticket_target_policy: base_plus_entry_spread_v1`, `stop_spread_policy: sell_ask_entry_only_exact_risk_v2` and `new_ticket_exit_policy`. In each symbol’s `target_preview_by_side`, read the BUY or SELL spread, indicative initial goal and ladder amounts for the displayed quote. These are not promises for later tickets. For an actual send, `demo_result` records `send_bid`, `send_ask`, final SL and spread buffer, planned and final quote spread, `configured_target_usd`, `entry_spread_usd`, `initial_goal_usd` and `planned_tp_profit_usd`. Compare the quote and requested TP to the deal price and verified ticket protection. The observer is read-only: it does not amend tickets. Armed logs distinguish `profit_ladder_enrolled`, `profit_ladder_step` (both prices verified on the broker ticket), `profit_ladder_deferred`, and `profit_ladder_broker_barrier` (observed touch, **not** a confirmed fill). The scanner’s account state stores original bases, the frozen pre-ladder policy, observed profit quote, confirmed pairs and pending requests in `ladder_by_ticket`. A lost reply/crash never triggers a blind resend. Both broker levels must match the pending pair to confirm; unresolved requests or unexpected external edits block new entries for review. Do not delete the journal to clear a hold.

`fixed_tp` uses the same base-plus-spread goal for the initial TP and fixed cash exit, without ladder amendments. It is **not** the default. The ladder calculation is independent of account type; the shipped order bridge still refuses real-money accounts. This is not a live-account rollout.

## Rate and symbols

| Rate | Requested count per eligible label | At 0.09 lot and $10 **base** each (USD totals exclude spread cushions) |
|---|---:|---|
| Slow | 10 | 0.90 lot / $100 base |
| Medium | 4 if it fits, else 3 | 0.27–0.36 lot / $30–$40 |
| Fast | 7 if it fits, else 6 | 0.54–0.63 lot / $60–$70 |

If even the minimum count cannot fit, **skip the whole signal**. No smaller surprise order. MT5 sends are sequential, not atomic; if a later leg fails, previously filled protected tickets remain and the bot does not retry or top up the batch. One active batch per **symbol**. Same-close eligible pairs that oppose each other skip; otherwise the highest qualifying parent owns it. All share account-wide limits and free margin; preflight shows the maximum tickets/lots across the selected lanes. Three full slow batches can total 30 tickets on one symbol. Rate changes batch size, not signal sensitivity or poll speed.

Default: only **XAUUSD** selected. Configured keys also include **EURUSD, AUDUSD, GBPJPY, GBPUSD, USDJPY, NAS100USD, US30, US500, DE40, DE30, JP225, 1HZ100V**. New entries require the [shared per-symbol schedule](ENTRY_SESSIONS.md); the legacy `1HZ100V` profile has **no** approved entry window. DE40 and DE30 both map to `DE30m`; select at most one. On the connected Exness terminal, AUDUSD and GBPJPY resolve to `AUDUSDm` and `GBPJPYm`. Their Hull profiles use 1 lot per ticket, the EURUSD $10 base and slow rate; the GBPJPY spread cap is symbol-specific. FX/metals/CFDs/synthetics can have very different lot steps, trading hours and cash values. Resolver requires exactly one matching configured alias in the terminal catalogue; otherwise set `hull.symbols.<KEY>.broker_symbol` to one exact Market Watch name. No substring guessing or automatic trading of everything in Market Watch. To use another instrument, explicitly add a profile with its exact name, lot, target, rate, `pip_size` and appropriate spread cap. A profile alone does not grant entry hours: the key and verified broker name must also agree in `src/bot/config/entry_sessions.yaml`. `extra_pips: 0` means pip size does not affect the default SL. A template 0.01 lot is not necessarily valid: check specifications rather than silently rounding size up. Live calculations use broker tick size, digits, stop/freeze distance, volume limits, margin and account-currency conversion. One safe process can accept different profiles:

```powershell
python -m bot.cli hull-preflight --profile XAUUSD:0.09:10:fast --profile EURUSD:0.01:8:slow
python -m bot.cli trade-all --profile XAUUSD:0.09:10:fast --profile EURUSD:0.01:8:slow
```

Or apply one set of overrides: `--symbols EURUSD GBPUSD --lots 0.01 --target-usd 10 --rate medium`. Do not mix these with `--profile`. Omit `--timeframe` to use the explicit `hull.eligible_timeframes` list. An override can only select an already approved timeframe.

## Hull portfolio limits and broker funding

The default applies to every selected Hull profile. There is **no bot-imposed ceiling** on open ticket count, gross lots or modeled USD loss at stops, and **no extra percentage of equity** is set aside as a margin reserve. Exposure and margin checks use the actual lots and chart stops, not a projected TP or future ladder lock. For example, an otherwise eligible Gold batch of 10 × 0.10 lots with $204.87 modeled risk per ticket records **$2,048.70** in `batch_preview.risk_batch` rather than rejecting it against the old $1,000 cap. This is potential loss, not required margin; actual loss can be larger.

**No Hull drawdown halt or per-minute send limit.** A drop of $75 (or more) below a previous equity peak no longer blocks the next funded entry or the rest of an eligible batch. Entry requests and per-ticket ladder amendments no longer compete for 40 slots per minute. Eight eligible slow profiles could request 80 entries in a minute, plus ladder updates; MT5 can still throttle or reject them. Orders are sequential, require valid positive equity and are never guaranteed to fill. Losses can accumulate without an automatic drawdown stop.

**Broker funding is still mandatory.** Before entry (and again for the remaining tickets), the bot subtracts the full requested count × MT5’s single-ticket margin estimate from current free margin. A funded batch can leave less than 60% of equity free, even zero; an unfunded batch cannot proceed. In armed demo mode each ticket also needs an accepted MT5 `order_check` with valid projected free margin. Only a confirmed broker result establishes a fill. Chart-study results are not Dynamic Hull executions.

| New log reason | What to check |
|---|---|
| `insufficient_free_margin` | Free margin is negative now or would be negative after funding. |
| `margin_floor` | Funding fits, but free margin would be below an **explicit positive** Hull reserve in this config. This reason cannot occur with the shipped 0% reserve. |
| `no_margin_estimate` | Required margin data were missing or invalid. No send. |
| `broker_precheck_rejected` | MT5 returned no check result or rejected the request; see the recorded `retcode` when available. |

For the first three reasons, `margin_veto` identifies the stage (`first_plan`, `next_leg`, `pre_send` or `order_check`), number of legs checked, equity, current/projected free margin and required reserve in USD. An unknown value is `null`. A batch preview is **not** a promise of a fill.

**Updating an existing Windows demo config:** `git pull` does not edit the ignored `config.demo.local.yaml`. Positive values for **the four optional keys below** still take effect. To adopt their defaults, edit just these four keys **inside the existing `hull.limits` mapping** (or remove just those keys to inherit their code defaults). Do not add a duplicate mapping, replace the file or assume legacy top-level `risk` / `zones` fields are Hull controls.

```yaml
# Within hull.limits only; keep its other settings.
max_positions: null
max_total_lots: null
max_open_risk_usd: null
min_free_margin_pct: 0
```

Use read-only `hull-preflight --paired-confirm --config config.demo.local.yaml` **with the same profiles** before a new demo start. Verify the three `null` values and `0` in `limits`; `review_before_demo` lists any of these four limits that are enabled. Review the account, exposure and other warnings as usual. Preflight and `started.summary.retired_limits` list `drawdown_usd` and `max_orders_minute`; neither is active even if an older local Hull YAML explicitly sets it. Old journal `guard` and `sends` data have no effect. Leave the journal intact: consumed signals, manual holds and pending ladder requests still matter. A running scanner does not reload these changes, and an update does not replay past setups.

## Windows demo checklist

1. Install 64-bit Windows Python 3.11+ supported by the MT5 package. Start MT5 and log in locally to a dedicated **USD hedging DEMO** account. Identify running scanners and exposure first. Never interrupt an armed/unknown process, modify its checkout or touch manual positions; stop and ask. Follow the [checkout/version check](../README.md#check-the-checkout-before-reusing-an-observer). Don't change permissions on a real account. In the project root:
   ```powershell
   python -m pip install -e ".[test]" MetaTrader5 tzdata
   python -m pytest -q
   python -m bot.cli validate-config
   python -m bot.cli hull-preflight --profile XAUUSD:0.09:10:slow
   ```
   The preflight reads symbol specs, permissions, existing exposure, and the most recent historical label/anchor for each lane, including the native trigger histories. These historical labels are **never submitted**. Investigate wrong/ambiguous names, unavailable history, stale/wide spreads and invalid volume rather than bypassing guards. If auto-detection picks the wrong terminal, set `mt5.terminal_path` in your local config.
2. `python -m bot.cli trade-all --profile XAUUSD:0.09:10:slow` is a **read-only observer**, not a shadow P&L backtest. Compare `lane_mss_exit`, `exit_request`, `lane_exit_confirmed`, `structure_break`, `pair_arbitration`, `pair_trigger_exit_signal`, `closed_label`, `entry_side`, `entry_reason`, `anchor_s`, `raw_sl` and `batch_preview` with the matching candle/tick on **your broker feed**. That need not be the OANDA feed pictured. A risk veto logs per-leg loss and the minimum batch's loss separately. `--max-iterations` allows a bounded smoke run; a one-iteration run only warms history.
3. For a **new** local demo config, copy `config.yaml` to the ignored `config.demo.local.yaml` and set `run.mode: demo` in the copy. If one already exists, **do not overwrite it**; migrate the two [paired YAML keys](HULL_PAIRS.md) and review the [four Hull defaults](#hull-portfolio-limits-and-broker-funding). Re-run read-only preflight on the local file with the same profiles and review the report. Only after separate explicit arming approval and verification of demo-terminal permissions, launch **one** process:
   ```powershell
   python -m bot.cli trade-all --paired-confirm --config config.demo.local.yaml --profile XAUUSD:0.09:10:slow --arm-demo
   ```
   Paper is the shipped default. No `--arm-demo` = no sends. A real, contest, non-USD, netting, disconnected or changed account cannot arm. All examples are operator instructions: the development sandbox did not connect to a terminal or place orders.
4. Check the **effective** limits, not just the checked-in YAML. Hull has no automatic peak-to-current or daily loss stop (`daily_loss_usd` is **not** enforced). Do not resize tickets, move the SL or change an operator-selected limit to force an entry.
5. Logs: `reports/hull_runtime/<account-hash>/{shadow,demo}.jsonl`; recorded broker deals separate profit, fee, commission and swap. Durable intent, per-ticket goals, ladder confirmations and manual hold live in `~/.moneyprinterbot/hull/<account-hash>/state.json`. An OS lock prevents two armed Hull scanners under the same Windows user from trading the same account. Foreign positions or pending orders block new batches. A rejected/uncertain/partial send may need manual reconciliation; **do not delete state to retry**. Check actual tickets/deals, full size and protection, preserve a backup, then remove only a resolved `manual_hold` while stopped. Keep symbol whitelist/magic unchanged while tickets are open.
6. Ctrl+C stops the software; broker SL/TP remain attached. The `KILL` file in the **bot's working directory** blocks new orders and requests closure of owned, allowlisted positions while the scanner is running and quotes/permissions permit. No ordinary session or momentum exit; configured session/rollover gates block new entries only. Review market-specific hours, especially CFDs/synthetics. Software exits cannot work while disconnected.
