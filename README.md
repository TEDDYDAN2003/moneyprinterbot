# Dynamic Hull 16 · MT5 demo desk

This is the **Dynamic Hull scanner, its trade journal, and its read-only chart tools**. The checked-in [config](config.yaml) observes; it does not send orders. Broker access requires your Windows MT5 desktop on a dedicated **USD hedging demo** account. No live-money mode, cloud database, webhook or trained model is needed.

[Previously packaged desk (verify its policy before use)](downloads/Dynamic-Hull-Desk.zip) · [Operating guide](docs/HULL_DEMO.md) · [Journal setup](HullJournal/START-HERE.md) · [Profile picker](docs/Hull-demo-start.html)

## How a trade works now

See the [intrabar market guide](docs/HULL_INTRABAR.md) for the current source policy. Validate the actual effective timing and pairs in preflight. Private YAML and older packaged downloads may still select the closed route.

| Parent (owns the batch) | Entry chart (owns the crossover and initial stop) |
|---|---|
| M5 | M3 |
| M10 | M5 |
| M15 | M10 |

A fresh forming BUY cross needs bullish closed structure on the trigger and its own parent; SELL is the mirror. Freeze the intersection of the selected Hull lines, not the fill. If it is outside the trigger candle’s Bid high–low range, apply it immediately when tighter than the confirmed child HL/LH stop. If inside, keep the swing SL and TP. At that candle’s actual close, tighten on the favorable side (BUY above, SELL below); request exit on a touch, wrong-side close or original-source uncross. Never loosen a stop.

The default market runner also accepts a **fresh, independently confirmed closed cross** not already consumed intrabar. An opposing owned batch already closing must be verified flat within the close’s original age window. No stale replay, stacking, or partial-batch top-up. A later same-candle live recross requires a fresh tick after flat. Parent MSS exits and the opposing child/parent signals remain independent. [Timing, stops, re-entry and audit](docs/HULL_INTRABAR.md) · [explicit closed market route](docs/HULL_PAIRS.md) · [optional direct-retest limits](docs/HULL_RETEST.md).

**Tick shifts and target settings.** A paired market batch rechecks only its remaining unsent legs, on the original deadline. A new quote is not itself a new batch. Open [Journal → Symbol targets](HullJournal/START-HERE.md#keep-per-symbol-targets-and-preview-the-spread) to save a USD base per enabled symbol and preview the broker-valued spread for each side. The Windows launchers use your private enabled list and effective targets, not hard-coded `$10` profiles. Explicit CLI overrides remain per-run.

**Know the size before arming.** The paper template selects only XAUUSD at 0.10 lot and a $10 base **per ticket**. Slow requests 10 tickets; medium 4 or 3; fast 7 or 6. A fully filled slow gold batch is 1.00 lot, **not three batches**. The gross target adds a spread cushion in USD (entry quote for market orders; frozen placement quote for limits). The existing [per-ticket profit ladder](docs/HULL_DEMO.md#profit-ladder-for-new-tickets) and [entry sessions](docs/ENTRY_SESSIONS.md) still apply. The stop is a price rule, not a cash-loss ceiling. Configured funding/risk, drift and session checks still apply; no return or exact fill is promised.

**Return-to-entry stop (new ladder tickets):** First the executable quote must show a profit (Bid for BUY, Ask for SELL). Only on a **later** return to or through the actual fill can the scanner tighten the stop to the midpoint between the fill and the **original broker SL**, rounded toward entry. TP stays unchanged. A BUY filled at 100 with SL 90 can move to 95. A SELL filled at 100 with raw Bid-chart SL 110 and spread 2 starts with broker SL **112** and can move to **106**. Spread translates the initial SELL candle stop **once**; neither the midpoint nor a cash-priced ladder rung adds it again. A stronger legal rung takes priority. Existing tickets retain their recorded rules; no installed SL is widened. [Timing, limits and broker confirmation](docs/HULL_DEMO.md#profit-ladder-for-new-tickets).

## Start safely on Windows

Open PowerShell in the **bot root** (the folder containing `config.yaml` and `HullJournal`). Keeping the same working directory matters: the scanner appends its audit files relative to it. For a new install:

```powershell
if (-not (Test-Path .\.venv\Scripts\python.exe)) { py -3 -m venv .venv }
$py = '.\.venv\Scripts\python.exe'
& $py -m pip install -e ".[test]" MetaTrader5 tzdata
if ($LASTEXITCODE -ne 0) { throw 'Install failed' }
& $py -m bot.cli validate-config --config config.yaml
if ($LASTEXITCODE -ne 0) { throw 'Invalid config' }
& $py -m pytest -q
```

These commands do not connect to a terminal or send orders. For a **read-only** check on your signed-in MT5 computer, run `& $py -m bot.cli hull-preflight --config config.yaml`. Review the account, actual broker names, session map, batch sizes, stop/target policies, limits, open exposure and `review_before_demo`. A preflight does **not** start a scanner or write a session log. `trade-all` without `--arm-demo` is the observer; `trade` and `demo` are aliases for the same guarded scanner, not different strategies. Use one observer and **only** your selected profiles. `export-ticks` is a read-only quote exporter, not a backtest.

### Check the checkout before reusing an observer

Do not update, stop or replace an armed/unknown process or one managing open tickets. Upgrade while flat with no unresolved sends. Keep your existing checkout path, ignored private YAML, account magic, `reports/hull_runtime`, home-directory state and journal output folder. Back up the entire journal output with **only the reporter stopped**. Do not reset Git or delete a lock, reservation or SQLite file to make a start succeed.

A pull or ZIP extraction does not update a running process. In the private YAML, set `hull.execution_mode: paired_structure`, `hull.eligible_timeframes: [M5, M10, M15]`, `hull.entry_timing: by_route`, `hull.entry_route: market_next_quote` and `hull.indicator.stop: cross_candle` without overwriting lots, aliases, caps or emergency settings. A legacy `pilot.daily_flatten_time` / `daily_reopen_time` still supplies the same maintenance blackout; the new template calls these `sessions.maintenance_start` / `maintenance_end`. Conflicting old/new bounds are rejected. Other retired research sections are not execution controls. [Choose timing without changing other settings](docs/HULL_INTRABAR.md#choose-timing-without-changing-other-settings).

### Standing instructions for the desktop agent

On a start, restart, profile change or arming request, read [the profile picker](docs/Hull-demo-start.html) and the [desktop procedure](docs/HULL_DESKTOP.md). A pasted selection or [`HULL START trio-slow`](docs/HULL_START_PROMPT.md) requests **observation only** with those exact profiles. Compare the latest `started.summary.checkout_head_at_start` and `source_module` with the intended build. Look for `entry_timing: intrabar_cross`, `entry_quote_policy: first_verified_cross_tick_v1` and `new_ticket_stop_policy: confirmed_trigger_range_hl_lh_v1`, `stop_spread_policy: sell_ask_entry_only_exact_risk_v2`, with `pair_history_ready: true` in preflight. Sending demo orders requires a **separate, explicit `ARM DEMO`**, fresh preflight with that private demo config and unchanged profiles. Never trade to test logging.

## Where to find a decision or a trade

| File | What it tells you |
|---|---|
| `reports/hull_runtime/<account-hash>/shadow.jsonl` | Append-only **observer** decisions. A `batch_preview` or `shadow_plan_only` is not a trade. |
| `reports/hull_runtime/<account-hash>/demo.jsonl` | Append-only **armed demo** audit: `intrabar_arbitration`, `intrabar_pair_cross` (or `pair_arbitration`, `closed_label` for confirmed timing), `pair_trigger_exit_signal`, `label_decision`, `demo_result`, exits, ladder activity and `broker_deal`. `started.summary.audit_log` prints the resolved path. |
| `~/.moneyprinterbot/hull/<account-hash>/state.json` | Private execution state: reservations, lane ownership, pending sends and ladder records. This is **not** the analytics journal; do not edit/delete it to replay a signal. |
| `HullJournal/data/journal.sqlite3` | Default private reporter checkpoint, bound to account and magic. An existing custom `--out` folder should stay in use. |
| `tickets.csv` / `setups.csv` in the reporter output folder | One row per fully closed broker position / completed batch. Open, skipped, rejected and shadow signals are **not** realized P/L. Don’t add the ticket and setup totals together. |

**Intrabar market:** follow the `q…` receipt as described in the [audit guide](docs/HULL_INTRABAR.md#audit-and-limits). The trigger tick is not a close timestamp.

**Follow a closed-cross ticket:** `closed_label` identifies the confirmed candle and its wick → `label_decision.outcome` explains acceptance or veto → `demo_result` records the exact request, send quote and receipt deal ID. The journal joins the entry by deal ID or reads its original `p<parent>x<trigger>_<close-seconds>_<leg>` tag, then settles using MT5 deals by position ID. Compare `signal_close_utc`, `send_quote_utc` and `open_time_utc`: these are the **signal**, **quote** and **actual broker fill**, not interchangeable timestamps. With a verified current receipt the journal includes the wick, frozen SL and any failed-counter-MSS exception. Historical `i…` trades remain intrabar trades; older closed trades are not assigned the new stop by guesswork. Missing metadata stays blank. Net is broker profit + signed commission + swap + fees.

**Sync without starting the bot.** With MT5 on the original account, in the bot root:

```powershell
$py = '.\.venv\Scripts\python.exe'
$out = '.\HullJournal\data'  # or the EXISTING directory containing your journal.sqlite3
& $py .\HullJournal\journal.py --repo . --out $out --once
```

For continuous sync, use `--open-browser` **instead of** `--once`, with only one writer for that folder. The local dashboard is at `http://127.0.0.1:8765/`. Pass `--magic` if you changed Hull’s default `26111825` and `--terminal-path` when needed. `--repo` points at the scanner’s working directory; it reads **only** this account’s `demo.jsonl`, never `shadow.jsonl`. The reporter can still journal broker fills without that log; it cannot reconstruct the missing wick or send quote. [Journal upgrade, backups and troubleshooting](HullJournal/START-HERE.md).

## Other supported tools

- [MT5 Dynamic Lanes](MT5/DYNAMIC-LANES.md): read-only chart monitor, not an Expert Advisor.
- [TradingView / Free Chart replay](docs/HULL_CHART_REPLAY.md): visual study with its **own** entry and exit accounting. Its chart rates are not bot fills or a backtest of this stop.
- [Wickless M15](docs/WICKLESS.md): independent strategy, JSON config and `python -m bot.wickless` command; no shared tickets. [Wickless Desk](WicklessDesk/index.html).
- [Session benchmark](research/session_volatility/README.md) and [Wickless evidence](research/wickless_m15/README.md) are retained because the dashboard and regression tests use them. The old ML/pilot, zone and archived Hull replay executors, raw research dumps and obsolete chart downloads were retired. Their tracked source is recoverable from Git revision `9b1447b`; it is not part of current execution. Existing operator records were not cleanup targets.

## Offline verification

From the root with the project dependencies installed (no MT5 required):

```text
python -m pytest -q
python -m pytest -q HullJournal/tests
python MT5/qa/check_dynamic_lanes.py
```

The last command needs a C++17 compiler; it checks the shipped MQ5 calculation against Python on synthetic candles, not a native MetaEditor build. The [scanner → journal tests](tests/test_hull_journal_bridge.py) exercise the file paths above, old and new tags in the same checkpoint, partial batch settlement and repeat syncs. None of these tests measures profitability.
