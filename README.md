# Dynamic Hull 16 · MT5 demo desk

This is the **Dynamic Hull scanner, its trade journal, and its read-only chart tools**. The checked-in [config](config.yaml) observes; it does not send orders. Broker access requires your Windows MT5 desktop on a dedicated **USD hedging demo** account. No live-money mode, cloud database, webhook or trained model is needed.

[Download the current desk](downloads/Dynamic-Hull-Desk.zip) · [Operating guide](docs/HULL_DEMO.md) · [Journal setup](HullJournal/START-HERE.md) · [Profile picker](docs/Hull-demo-start.html)

**Earlier entries on M1:** [M1 trigger, parent structure and Windows restart](docs/HULL_M1.md). A confirmed M1 cross uses M5/M15/M30/M45 parent direction, with one symbol batch and immediate observed opposite-M1 protection. `--m1-confirm` selects this mode; do not add M1 as an independent trading lane.

## How a trade works in legacy parent-close mode

1. **Watch one instrument and one timeframe at a time.** Each selected symbol has independent M15, M30 and M45 lanes with their own structure and positions. M45 is three consecutive M15 candles aligned to the broker’s D1 open. An incomplete group is not a closed M45 candle.
2. **Confirm the cross.** The close-based Hull B must cross above the selected Hull A for a BUY, or below for a SELL. In the default Dynamic/16 setup, A uses lows in bullish structure and highs in bearish structure. Only trade in the confirmed direction; never enter on neutral structure or on an MSS (market-structure shift) alone. A cross that vanishes before the candle closes opens **nothing**.
3. **Apply the first-cross rule.** Normally skip the first otherwise-valid aligned cross after an MSS. If a counter-move made **no continuation BOS** (break of structure) and a confirmed MSS restores the established trend, its first valid cross **on a later close** can qualify. A false bullish shift need not cost the next valid bearish SELL. The rule is mirrored for BUY, with no time limit and no requirement for a countertrend trade. [Worked structural cases](docs/HULL_STRUCTURE.md#first-cross-after-an-mss-attempted-reversal-vs-failed-counter-move).
4. **Enter on the next candle, not its closing price.** For an eligible, flat lane, the first usable broker quote in the *immediately following candle* can price the batch. BUY at ask; SELL at bid. For example, an M15 BUY confirmed at 10:15 can use a fresh 10:15:00.400 quote; it does not enter at 10:14, wait until 10:30, or guarantee the opening print. Defaults: signal age ≤8 s, quote age ≤4 s. Per-symbol [entry sessions](docs/ENTRY_SESSIONS.md), spread, funding, exposure, broker permissions and per-leg checks still apply.
5. **Attach the stop in the opening request.** BUY: below the **cross candle’s low**. SELL: above its **high**. With default settings the cushion is the larger of **0.1 × the completed lane ATR14** and **one broker trade tick**; round outward. Freeze this price for the entire batch. Never substitute a prior swing or the developing entry candle. If the stop is crossed, too close, or the tick grid changes, skip the unsent legs; never widen the stop or top up a partial batch. [Price and audit example](docs/HULL_DEMO.md#which-candle-supplies-the-stop).
6. **Manage filled tickets.** A verified opposite cross (even intrabar) protects the same lane’s opposing tickets; a confirmed MSS exits its own lane. New entries may be blocked while these exits still run. The default per-ticket target is **base USD + that leg’s entry spread in USD**. At +70% of this frozen goal the profit ladder first attempts to lock +60% and extend TP to +120%, subject to broker acceptance. [Ladder and existing-ticket policy](docs/HULL_DEMO.md#profit-ladder-for-new-tickets).

**Know the size before arming.** The paper template selects only XAUUSD, at 0.10 lot and a $10 base **per ticket**. Slow requests 10 tickets per eligible lane; medium 4 or 3, fast 7 or 6. A fully filled slow gold batch is 1.00 lot on one lane, up to 3.00 lots if all three fill. The stop is a price rule, not a cash-loss ceiling. Optional ticket, lot and modeled stop-risk caps default to `null`; the extra free-margin reserve is 0%. The bot still checks actual funding. A wide candle, spread, gaps, fees and slippage matter. There is no proven return or enforced daily loss cap.

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

A pull or ZIP extraction does not update a running process. In the private YAML, set `hull.entry_timing: confirmed_cross_next_bar` and `hull.indicator.stop: cross_candle` without overwriting lots, aliases, caps or emergency settings. A legacy `pilot.daily_flatten_time` / `daily_reopen_time` still supplies the same maintenance blackout; the new template calls these `sessions.maintenance_start` / `maintenance_end`. Conflicting old/new bounds are rejected. Other retired research sections are not execution controls. [Full migration and required settings](docs/HULL_STRUCTURE.md#upgrade-the-local-configuration).

### Standing instructions for the desktop agent

On a start, restart, profile change or arming request, read [the profile picker](docs/Hull-demo-start.html) and the [desktop procedure](docs/HULL_DESKTOP.md). A pasted selection or [`HULL START trio-slow`](docs/HULL_START_PROMPT.md) requests **observation only** with those exact profiles. Compare the latest `started.summary.checkout_head_at_start` and `source_module` with the intended build. Look for `entry_timing: confirmed_cross_next_bar`, `entry_quote_policy: first_fresh_post_close_tick_v1` and `new_ticket_stop_policy: confirmed_cross_candle_wick_v1`. Sending demo orders requires a **separate, explicit `ARM DEMO`**, fresh preflight with that private demo config and unchanged profiles. Never trade to test logging.

## Where to find a decision or a trade

| File | What it tells you |
|---|---|
| `reports/hull_runtime/<account-hash>/shadow.jsonl` | Append-only **observer** decisions. A `batch_preview` or `shadow_plan_only` is not a trade. |
| `reports/hull_runtime/<account-hash>/demo.jsonl` | Append-only **armed demo** audit: `closed_label`, `live_hull_cross`, `label_decision`, `demo_result`, exits, ladder activity and `broker_deal`. `started.summary.audit_log` prints the resolved path. |
| `~/.moneyprinterbot/hull/<account-hash>/state.json` | Private execution state: reservations, lane ownership, pending sends and ladder records. This is **not** the analytics journal; do not edit/delete it to replay a signal. |
| `HullJournal/data/journal.sqlite3` | Default private reporter checkpoint, bound to account and magic. An existing custom `--out` folder should stay in use. |
| `tickets.csv` / `setups.csv` in the reporter output folder | One row per fully closed broker position / completed batch. Open, skipped, rejected and shadow signals are **not** realized P/L. Don’t add the ticket and setup totals together. |

**Follow one ticket:** `closed_label` identifies the confirmed candle and its wick → `label_decision.outcome` explains acceptance or veto → `demo_result` records the exact request, send quote and receipt deal ID. The journal joins the entry by deal ID or reads its original `h<tf>_<close-seconds>_<leg>` tag, then settles using MT5 deals by position ID. Compare `signal_close_utc`, `send_quote_utc` and `open_time_utc`: these are the **signal**, **quote** and **actual broker fill**, not interchangeable timestamps. With a verified current receipt the journal includes the wick, frozen SL and any failed-counter-MSS exception. Historical `i…` trades remain intrabar trades; older closed trades are not assigned the new stop by guesswork. Missing metadata stays blank. Net is broker profit + signed commission + swap + fees.

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
