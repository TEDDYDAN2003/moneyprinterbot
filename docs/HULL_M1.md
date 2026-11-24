# Dynamic Hull: M1 trigger, higher-timeframe structure

**Previous release, not the active launch policy.** See [paired triggers and restart](HULL_PAIRS.md) for the current scripts. This page documents historical M1 behavior and explicit compatibility mode.

**Scope:** optional Python execution mode for the existing USD hedging demo desk. It replaces parent-candle entries; it does not run an extra M1 trading lane. Other scripts, private data and historical tickets are not converted. Ordinary configs without the new option keep `parent_close`. The scripts shipped at `dc080b4` selected M1; the current scripts do not.

## The two clocks

1. Each authorized parent (M5/M15/M30/M45, restricted by your private allowlist/CLI) supplies its **last fully closed native structure** as of the M1 close. M45 still requires three broker-day-aligned M15 candles. Missing, stale, warming or future context cannot authorize a batch. A neutral parent cannot qualify. There is no M1 structure vote and no requirement that the parent Hull is currently crossing.
2. One native M1 price stream per symbol builds the same period-16 curves. With Dynamic source, a bullish parent selects the M1 Low Hull A; a bearish parent selects the M1 High Hull A. Hull B uses closes. Both sides of a crossover compare the **same source**. Entries require an actual cross on the **completed M1 candle**, not just B above/below A. A forming cross that disappears cannot open a trade. An M5 live cross does not prove an M1 cross ever occurred.
3. Each parent's first otherwise-valid aligned M1 cross after an ordinary MSS is skipped. A confirmed return to the parent's established trend with **no intervening counter-BOS** can waive its first valid **later** M1 cross. An MSS never opens a trade. **No new continuation-BOS entry filter.** No replay on attachment, restart, missing history or recovery; M1 reservations are separate from legacy first-cross records.
4. Of eligible parents, choose the **highest timeframe**. If eligible parents signal opposing sides at this close, skip. All four need not agree. **One batch at a time per symbol**, never one batch per parent, no fallback to a lower parent after a veto, no duplicate on the subsequent parent close. An existing position on this symbol can block a new batch; no legacy position is relabelled. Other symbols still share the actual account's safeguards.
5. The first fresh observed bid/ask in the **immediate successor M1 candle** may price the batch; it is not a promise of the opening price. At 11:53 a closed M1 SELL may qualify using M15's last closed structure even though the 11:45–12:00 M15 is unfinished. Spread, session (signal, quote, submit), funding, stop distance, quote freshness, one-tick drift, equity/portfolio checks, demo permission and KILL remain in force. No backfill, requeue or late top-up. The user’s existing lots and rate still determine how many tickets one batch can contain.

**Initial SL:** BUY below the completed M1 cross candle low; SELL above its high. Buffer = max(one broker *trade tick*, configured ATR multiplier × completed M1 Wilder ATR + configured extra pips in price units). Round outward on the verified broker grid and freeze for every leg. Never use the previous M1 candle, the parent candle or a forming wick. If the stop is breached/too close at the executable quote, or the grid changes, skip; never widen it. Attached broker SL/TP, cash goal and ladder policies are otherwise unchanged.

**Fast exits:** A verified raw opposing M1 cross can request immediate ticket-specific closure **before the minute closes**, even outside entry hours. A cross on an active parent's selected M1 curve broadcasts to opposed M1-origin tickets on the **same broker symbol across parents**; the original entry curve also protects its own tickets if parent history is unavailable. No stop or aligned entry candidate is necessary. A confirmed M1 cross is also checked. Entry-source/position fingerprints, durable per-side fences, write-ahead exit intents and partial-close reconciliation protect against duplicate sends. Never close legacy, unattributed, other-strategy, same-direction or other-symbol tickets via this new rule. Original parent opposite/MSS and emergency protections continue independently. A live cross can flicker and exit a trade that would later recover.

## VS Code / PowerShell restart checklist

1. **While flat**, with no pending orders, unresolved sends or ticket management in progress, stop only the identified old scanner. Do not force-close tickets or remove locks. Back up your private config, account state, logs and journal. Work in the *same* Windows repo root on clean `main`; `git pull --ff-only origin main` for a published release (or use the provided offline fast-forward instructions). Do not overwrite `config.demo.local.yaml`.
2. In **your existing private YAML** keep lots, aliases, magic, limits, sessions and the cross-candle stop. Change only these `hull:` keys if you want all four parents (do not duplicate the whole `hull:` block):

   ```yaml
   execution_mode: m1_parent_structure
   eligible_timeframes: [M5, M15, M30, M45]
   ```

   Do **not** add `M1` to the list. Keep `entry_timing: confirmed_cross_next_bar`, `indicator.structure_timeframe: null`, `indicator.stop: cross_candle`. `--m1-confirm` selects the same mode without editing the mode key; it **never expands the allowlist**. Other supported parents may be omitted intentionally. To restore legacy timing **while flat**, remove the override flag and set `execution_mode: parent_close`.
3. Preflight and observe first. Use the **same selections** for preflight/observer and any later demo session. These examples use the symbols and sizing configured in your private YAML; if you normally supply `--profile` overrides, include the same overrides on **all** runs.

   ```powershell
   $py = '.\.venv\Scripts\python.exe'; $cfg = '.\config.demo.local.yaml'
   & $py -m pip install -e . --no-deps
   if ($LASTEXITCODE -ne 0) { throw 'Install failed' }
   & $py -m bot.cli validate-config --config $cfg
   if ($LASTEXITCODE -ne 0) { throw 'Invalid config' }
   & $py -m bot.cli hull-preflight --config $cfg --m1-confirm
   if ($LASTEXITCODE -ne 0) { throw 'Preflight failed' }
   & $py -m bot.cli trade-all --config $cfg --m1-confirm
   ```

   Check `READ ONLY`, `execution_mode: m1_parent_structure`, `trigger_timeframe: M1`, intended `timeframes`, `m1_history_ready: true` in preflight, `new_ticket_stop_policy: confirmed_m1_cross_candle_wick_v1`, account, lots, history, and warnings. Verify `checkout_head_at_start`/`source_module`. Keep the observer only if observation is intended. To send demo orders, stop **that observer** first, then use the reviewed invocation in **one** scanner only, explicitly adding `--arm-demo`. Do not use the current paired-mode wrappers to start an M1 manager. Software exits cannot operate when the bot/MT5 is offline.

## See why a trade did or didn't happen

Use the `audit_log` in `started.summary`. `m1_arbitration` gives candidate parents, raw sides, skips, conflicts and the selected owner. `closed_label` is the accepted signal candidate, `label_decision.outcome` is the desk outcome, `batch_preview` is not a fill. `demo_result` has per-ticket request and result, parent and trigger TF, original wick/SL, send quote, and `close_to_detect_ms`, `close_to_submit_ms`, `order_send_roundtrip_ms`. The first two are wall-clock measurements; the last times the actual MT5 API invocation with a monotonic clock, not exchange-only latency. Compare leg 1 to later legs. An M1-closed setup that gets `quote_or_plan_changed` is still skipped. `m1_raw_exit_signal`, `exit_request` and `lane_exit_confirmed` distinguish observation, requested close, and flat verification.

The scanner caches closed parent bars until their boundaries, shares M1 arithmetic per symbol, and targets at most 100 ms of **idle sleep** between scans in M1 mode. It also checks M1 during its own batch and services other held symbols between legs. This is polling, not a latency SLA: CPU, terminal history publication, symbol count, market ticks, disk and broker round trips matter. An intrabar cross can go unobserved between samples. No bypassed checks or profitability claim.

**Journal:** `m<parent>_<M1-close-seconds>_<leg>` distinguishes new tickets from historical `h…` and `i…`. `timeframe` is the owner, `entry_timing` is `confirmed_m1_close`, and signal-bar times span one minute. Stop/send evidence needs a matched receipt; net P/L still needs closed broker deals. Keep the old journal folder/checkpoint. The supplied MQ5/Pine/Free Chart studies are **parent-chart references**, not M1 execution or fill displays. Use the scanner audit and journal for this mode.
