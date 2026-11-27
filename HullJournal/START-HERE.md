# Hull Ledger | Analytics edition · Windows quick start

**Active Python desk:** [intrabar market entries, trigger swing stops and private-YAML timing](../docs/HULL_INTRABAR.md). The journal recognizes both generations of paired ticket tags (see below), not a list of authorized lanes. No M1 trigger in paired mode; M30/M45 are historical/study frames, not active parents. [Previous M1 release](../docs/HULL_M1.md).

A read-only journal for the Hull bot’s **actual MT5 hedging-demo fills**. It does not send orders, subscribe to TradingView, change the scanner, or require a cloud/database account. A BUY arrow, shadow plan or attempted order is never a trade in this ledger.

## Upgrade an existing journal without losing history

Stop **only the journal reporter** in its own window. Keep a backup of your entire existing `data` folder, including the SQLite checkpoint. Do not delete the bot’s state, restart the scanner, or reset your CSVs. The kit contains **no** live `data` directory.

Either replace the reporter program files in the same folder, or extract the new kit separately and launch **its** `journal.py` with `--out` pointing at the previous `data` directory. Keep the same `--repo`, magic and terminal selection. The first successful poll reuses the checkpoint and regenerates the extra CSV columns. The account-scoped demo audit is re-indexed once to discover confirmed cross-candle receipts and past exit requests. This adds an evidence table and optional CSV columns; it does not reset the broker history cursor or change historical accounting. The new HTML can also import the old ticket/setup CSVs directly; download a filtered copy if you want enriched columns without running MT5. Use the local dashboard URL (refresh your tab), not a previously downloaded old HTML file.

## Start on the MT5 computer

1. In the full desk or extracted kit, open its **`HullJournal`** folder. Never extract over a running bot or an existing private `data` directory. Keep `journal.py`, `ledger.py`, `hull_analytics.py` and `Hull-Journal.html` together. Open MT5 Desktop and sign in to the intended **hedging demo** account. You do not need to enable Algo Trading, arm or restart the bot for this reporter.
2. Double-click **`Start-Journal.cmd`**. It checks the adjacent bot root first, then the existing bot Python environment if it finds `%USERPROFILE%\Downloads\xauusdm-directional-research\.venv\Scripts\python.exe`. Otherwise it tries the Windows Python launcher or `python`. It installs nothing. You may drag the scanner root onto the launcher, or run `Start-Journal.cmd "C:\path\to\bot" "D:\existing-journal-data"` to specify both the log root and the existing checkpoint. If both its own `data` and the bot root’s `HullJournal/data` contain different checkpoints, it asks you to choose rather than guessing. For a custom magic or terminal path use PowerShell below.
3. The launcher opens **http://127.0.0.1:8765**. Wait for **MT5 · live** and a last-read time. Keep the reporter window and MT5 running. The first history read can take longer. Stop **only the reporter** with Ctrl+C in its own window; this does not close any positions. No scanner needs to be started for a historical export.

**Alternative: PowerShell**, from the extracted folder containing `journal.py`, using the bot’s existing Python:

```powershell
$bot = Join-Path $HOME 'Downloads\xauusdm-directional-research'
$python = Join-Path $bot '.venv\Scripts\python.exe'
& $python .\journal.py --repo "$bot" --open-browser
```

**No bot environment?** Use your own supported 64-bit Windows Python 3.11+ and install the MT5 Python package into that interpreter once (or use a separate virtual environment):

```powershell
py -3 -m pip install -r .\requirements-windows.txt
py -3 .\journal.py --open-browser
```

**Timezone support:** If your existing Windows environment lacks IANA rules, install `tzdata` in that interpreter (e.g. `& $python -m pip install tzdata`). If unavailable the reporter still journals cash and writes `Timezone data unavailable`; the dashboard can derive sessions from UTC using the browser’s timezone database. It never substitutes a guessed fixed London/New York offset.

`--repo` is optional: it only reads the account-specific `reports/hull_runtime/<account-key>/demo.jsonl`. Without it, intact Hull entry comments supply the timeframe/leg. Extra log details, such as an initial requested SL/TP and the market-structure setup, may be blank. No account credentials go in the code or the CSV. With multiple terminals, pass `--terminal-path 'C:\…\terminal64.exe'` for the actual installation. Use `--magic` if you changed the default Hull entry magic **26111825**; do not reuse a magic shared with other strategies.

## Keep per-symbol targets and preview the spread

On the Windows bot PC, stop **only** any other reporter using the same output folder. From the full checkout open **`HullJournal\Start-Journal-Targets.cmd`**, or add `--target-config .\config.demo.local.yaml` to your normal reporter command from the bot root. Keep the same `--repo`, `--out`, terminal and magic. The ordinary launcher and standalone HTML still cannot save settings or run a preflight.

1. Expand **Symbol targets**. It lists exactly `hull.enabled`, with its lots, rate and private YAML base. Leave an override blank to inherit; enter a positive USD **base per ticket** and click **Save targets**. This writes only the target overrides to an ignored sibling file (e.g. `config.demo.local.targets.json`). It does **not** edit the YAML, enable a symbol, touch the journal checkpoint or change an open ticket. Keep both private files when moving installations. Unknown formats, conflicting edits and invalid amounts are refused, not reset to $10.
2. Click **Run read-only preflight**. The button runs only `hull-preflight` on this saved configuration and checks the returned account and magic against the live collector. For each symbol it shows the broker quote time and **BUY and SELL separately**: immediate spread cost in USD at those lots → indicative gross goal. For example, a $17.25 base plus $1.50 spread gives $18.75. No usable quote or conversion means a reason, not a guessed goal. This never starts a scanner or sends an order. Do not forward the local port.
3. The two PowerShell launchers now use **the private enabled list and its effective settings**, not the old fixed ten-symbol `$10` list. Review the list before any manual start. Nothing hot-reloads a running process. Explicit CLI `--profile` or `--target-usd` still overrides the base for that run; use identical arguments for preflight and any later run. An older/private dashboard with its own storage is not silently imported or overwritten: check its displayed values against this table.

**Historical evidence is separate.** A matched full opening can contribute `target_policy`, `configured_target_usd`, `entry_spread_usd`, `initial_goal_usd` and `planned_tp_profit_usd` to ticket detail and CSV. Retest receipts use the **placement** spread. A completed setup shows common per-ticket amounts only when they agree, plus `batch_initial_goal_usd` only when every component is evidenced. These are goals, not realized P/L. A preset change, preflight, signal, missing or conflicting receipt cannot supply a historical target. Keep the SQLite checkpoint: the upgrade re-indexes only available audit evidence.

## Intrabar paired market fills

`q<parent>x<child>_<trigger-ms>_<leg>` is an intrabar paired market fill group, not a closed cross or a retest. `entry_timing: intrabar_pair_cross` distinguishes it from older `i…` trades. Verified rows show the originating tick, source, confirmed trigger swing and confirmation time, frozen raw swing SL and actual broker request. A recross millisecond is a distinct setup, even in the same candle. The journal does not manufacture a signal close or a wick. Keep the existing checkpoint.

## Direct-retest fills

The [optional retest route](../docs/HULL_RETEST.md) uses `r<parent>x<child>_<close>_<leg>` and `confirmed_pair_retest_limit`, separate from `p…` market batches. An exact match on opening deal, order, position ID, symbol, side, magic, volume, price and time is required to attach retest evidence. A visible tag alone is not proof. New optional detail columns: `retest_limit`, `cross_candle_close`, `retest_segment`, `retest_expiry_utc`, `submit_to_fill_ms` (the last is ticket-only; differing component values stay blank). A completed retest setup needs the terminal batch settlement's exact fill IDs and fully closed broker volume. Without enough evidence a legitimate closed ticket can exist without a completed setup. A plan, pending acknowledgement, touch or expiration is never a ticket or P/L row. Old CSVs still import; keep the same SQLite checkpoint.

## Files and timing

| File | What it means |
|---|---|
| `data/tickets.csv` | One row for each **fully closed** Hull position, keyed by the stable MT5 position identifier, not by an individual fill. Partial exits wait for the last exit. |
| `data/setups.csv` | One row per **completed signal batch** (account, broker symbol, timeframe, exact trigger and direction). It sums the actually filled tickets, not the planned count. It waits for open or unresolved siblings. |
| `data/journal.sqlite3` | Local private checkpoint, linked deal history and exact audit metadata. It prevents duplicate rows after restarts. Do not edit the database or the bot’s state. |
| `Hull-Journal.html` | Open directly for a **labelled synthetic preview** or manual CSV import; use the local HTTP address above for automatic live updates. |
| `examples/` | Explicitly marked `SAMPLE` CSVs: three legacy parent-close M15/M30/M45 cross-candle batches plus older tagged examples. Never mixed into real exports. |

Default: a 30-day initial history read, then a broker poll approximately **every 2 seconds**, with the dashboard refreshing roughly every 2 seconds. Files are replaced on changes, not appended blindly. Speed depends on terminal connectivity, broker posting delay, volume and desktop load; this is polling, not an exchange-time guarantee. The CSV can update without the browser open. A market/session close does **not** close an open position.

For a very fast batch, the last ticket can close before the scanner has finished sending other legs. The setup waits for a terminal batch decision in the bot audit, or for **60 seconds from the signal** when such evidence is unavailable (the shipped scanner’s entry window is at most 30 seconds). The ticket row does not wait for this setup grace period. Missing known receipts or ambiguous same-window siblings withhold the setup instead of reporting a premature win.

## Use the dashboard

- Toggle **Tickets / Setups**. These are alternative ways of counting the same executions; do **not** add their totals together. KPI cards, the cumulative P/L curve and symbol bars follow the selected unit and filters.
- Filter by **symbol, timeframe, direction, outcome, and inclusive close-date range**. The search also accepts a position ID, setup ID, deal ID, exit reason or structure description. Click a symbol bar to filter to it; **Reset filters** clears the selection.
- Click a column heading to sort, click again to reverse. Open a row using the symbol link to inspect precise opening/closing timestamps, weighted prices, signed costs, holding time, broker reason and identifiers. A setup can drill down to its tickets if both views are loaded.
- **Export view** saves *all* matching rows in the current sort order, not only the visible page. Manual **Import CSV** replaces the display. Select one or both original CSVs **together** to load both views; mixed-account/currency files are rejected. File mode is not live. **Return to live journal** resumes polling if the HTML was served by the collector.
- CSV timestamps are UTC with milliseconds. **Nairobi / EAT (UTC+3)** is the default display; switch to UTC as needed. Close-date filters apply in that display zone. Broker IDs are kept as text; when importing to Excel, select **Text** for ID columns to preserve long integers.

## Sidebar analytics and reasons

- **Gross by symbol** and **Symbol performance** report signed pre-cost broker profit. Gross gains are the positive contributions and gross losses are the negative contributions **at the selected unit**; gross P/L includes both. Signed costs = commission + swap + fees; net includes them. Counts, gross, net, win rate and average net follow every active filter. Never combine the Ticket and Setup views. Click a symbol or group to inspect its records. **Export breakdown** downloads one aggregate row per visible group; **Export view** downloads the underlying filtered trades.
- **Symbol performance → Volatility by session** compares the median five-minute mid-price range and quoted spread on **11 distinct configured Exness markets** (September–October 2026). Click a session column to rank the markets, or switch to **Opening hours** to isolate the first hour in Tokyo, Frankfurt and New York. Values are in basis points: 1 bp = 0.01%, independent of lot size. `DE40` and `DE30` use the same `DE30m` series. `1HZ100V` is a 24/7 synthetic, not ranked against exchange hours. This is an offline reference, **not a session gate**.
- **Compare with the journal:** click a market, select **Setups** for one row per completed signal rather than per ladder ticket, and import your own CSVs or use live sync. **Export comparison CSV** pairs each entry window’s fixed benchmark with the filtered journal count, first/last entry, observed UTC dates, win rate, realized net and average net. The source column distinguishes the synthetic example from a journal import or collector. The default match is the exact archived broker symbol; explicitly filtering a known alias uses that exact journal name instead. Different contract names are not silently pooled. Quote history is not a record of your fills, and no trades means no performance estimate.
- **Trading sessions:** switch between the **entry** and **exit** timestamp (setup: first opening / last closing). Each local weekday window is start-inclusive, end-exclusive: Sydney 08:00–17:00 (`Australia/Sydney`), Tokyo 09:00–18:00 (`Asia/Tokyo`), London 08:00–17:00 (`Europe/London`), New York 08:00–17:00 (`America/New_York`). The city’s own DST rules apply. A record falls in one exact bucket, e.g. `London + New York`, or `Off-session`. A market-membership filter *includes* overlaps; the breakdown still counts each row only once. These are analytical FX activity conventions, not broker holidays/exchange sessions or entry gates. The clock inspects the latest filtered trade or shows current local windows using the computer clock. EAT/UTC changes displayed times and close-date filtering, not session membership.
- **Trade reasons:** entry shows the human-readable matched `closed_label` or `live_hull_cross` structure setup; no log evidence means **Not recorded**. Exit is the broker’s final exit reason or the actual Hull exit comment. **Stop loss / Take profit is not a guess based on whether P/L is red/green** (a raised SL can realize a gain). A setup with different ticket exit reasons shows `Mixed exits`; drill into its component tickets. Records without an actual fill do not appear.
- For a final expert `hull_exit`, a separately labelled **bot exit request** may show the bot’s recorded cause, e.g. `opposite_hull_label`, `kill` or `missing_protection`. The log contains no exit deal ID. The conservative match requires the same symbol and position ticket/identifier, success/partial-success return code, one unambiguous request timestamp within −5 to +15 seconds of the final broker exit, and no competing nearby Hull exit deal. Rejections, reused/changed tickets, multiple candidates, clock skew or missing logs leave this field blank. It does not replace the broker reason. Setup summaries with heterogeneous supporting notes show `Varies by ticket`.
- The collector CSVs retain their v1 base fields and add `entry_reason`, `open_session`, `close_session`, `bot_exit_request`, and `bot_exit_request_utc`. Last two require optional audit evidence. Older CSVs can supply the first three from their recorded structure type and UTC fields; an absent audit note stays absent.

## Accounting and attribution

- Owned **opening** deals establish trade ownership. Closing deals are matched by **position ID**, even when a manual, mobile, close-by, SL or TP exit has a different magic. The position must no longer be in the open snapshot and total exit volume must match entry volume. Mixed/unresolved histories are not guessed. Hedging demo only: netting reversals, cancelled executions or unsupported ownership patterns do not silently turn into wins/losses.
- **Net = profit + signed commission + swap + fee** on all broker deals linked to that position, including entry-side charges and partial exits. The result is win/loss/breakeven at the account’s currency precision. Spread is already in actual execution prices; it is not charged a second time. Account-level deposits, withdrawals or charges without a position ID are not allocated to a trade. This is a position journal, not a balance statement.
- **Open price / close price** are volume-weighted fill prices. Times are the first opening and last liquidation deal. Setup prices are lot-weighted across its same-symbol, same-side positions. `initial_sl` and `initial_tp`, when present, are the **initial requested** values from the matched bot audit, not a claim about final ladder levels. `frozen_cross_sl` is the raw Bid-chart stop: on a new SELL, `initial_sl` includes the single tick-rounded spread translation and can differ. The reporter verifies the placement/reference quote and the send quote before attaching this stop evidence. `exit_reason` uses the last closing deal; `Mixed exits` identifies a setup with different ticket exit reasons.
- **Paired tickets:** `p5x4`, `p10x8`, `p15x12` (and historical `p5x3`, `p10x5`, `p15x10`) include the trigger close and leg. The journal groups by the exact child, including for the corresponding `r…` limit tags; recognizing a historical or current tag authorizes no trading. `timeframe` is the owner; `trigger_timeframe` is the candle that crossed. Only a valid matched receipt proves `confirmed_pair_bias`, `parent_structure_close_utc`, wick, frozen SL and send quote. Never merge a paired setup with a historical tag on the same close.
- **Legacy confirmed next-candle tickets:** `h15_<close-seconds>_1` (likewise M30/M45) records the signal close, not the next quote or the fill. The matching `demo_result` supplies `initial_stop_policy`, `cross_candle_wick`, `frozen_cross_sl`, `send_quote_utc`, `send_bid`, `send_ask` and any `mss_skip_exception`. The quote is per ticket; `open_time_utc` and the volume-weighted price always come from broker deals. Ticket Details and filtered CSV export show this evidence. Completed setups show the common stop fields only when all settled legs agree; inspect individual tickets for their quotes.
- **Earlier records remain earlier records.** `i2_<trigger-milliseconds>_3` is a historical intrabar entry. Its trigger bid/ask, original bar boundaries and deliberately blank `signal_close_utc` keep the old meaning. Earlier `h…` tickets remain confirmed entries, **not automatically cross-candle-stop entries**. The journal preserves recorded v2/v4 structure reasons and `trend_hull_cross`; it does not infer a waiver, stop or quote from the date, an arrow or a preview. The new bot's `live_exit_only` projections have no entry side. Missing, unmatched or contradictory evidence is left blank or unattributed. No broker fill → no P/L row.
- The cash curve starts at zero for **the filtered closed records**. It is not account equity and includes no floating P/L. Win rate includes breakevens in its denominator. Profit factor uses winning and losing **net** amounts; ∞ means there are wins and no losses, not a guarantee.

## Recovery and reconciliation

- Use one running writer per output folder. The cache is bound to the hashed login/server identity, magic, currency and precision. A disconnect, account switch or failed API read pauses publication; no failed range advances the cursor. Restore the original account, or use a **new empty** `--out` directory for a different one. Last published files remain on disk.
- Restarts catch up from the saved cursor with an overlap; deals are deduplicated by broker deal ID. The rolling backfill window is rescanned hourly. A long-held position whose opening precedes the requested window is reconstructed from its full available history; its original signal window is queried for older siblings. Historical completeness is limited to **what the broker makes available**.
- Keep MT5 connected, make the required history available, and inspect **Quick guide → Collector diagnostics** for unresolved histories and unattributed tickets. Broker history is authoritative over simulated plans. A receipt outside the loaded period can appear as unmatched. Missing history, mixed ownership or knowingly missing siblings should be investigated, not manually relabelled as a win.
- If Excel locks a file, close it, or open a *copy*. CSV publication is atomic **per file**, not across two files. The live page switches to the new snapshot only after both exports succeed; failed writes retry. Use the HTML dashboard for live monitoring rather than editing its generated files.
- To reconcile older broker corrections, **stop this journal, not the bot**, and run one full tracked-position refresh before restarting. The cache retains earlier rows when the rolling window moves on. It does not invent deletions simply because a terminal stops returning older history. Compare a few ticket IDs, sizes and net amounts against MT5 **History → Deals** on first use.

```powershell
# From the extracted folder, using your chosen interpreter:
py -3 .\journal.py --once --backfill-days 90 --reconcile
py -3 .\journal.py --help
```

Use the same `--repo`, `--out`, `--magic` and terminal selection as your normal run. The ordinary local service binds only to loopback and exposes just the dashboard and its snapshot. Only the explicitly selected target editor adds target settings and read-only preflight; neither mode offers a trading API, account login or directory listing. The private files are not encrypted: protect the Windows folder. For a consistent backup, stop the reporter and copy the whole `data` folder. Do not publish private CSVs or the cache by accident.

## Verification, no MT5 needed

```powershell
py -3 -m unittest discover -s tests -v
```

From the full desk root, `python -m pytest -q tests/test_hull_journal_bridge.py` also exercises the real scanner log writer and CSV exporter together on a fabricated broker. The kit includes deterministic tests covering partial/manual exits, cost accounting, long-position backfill, late corrections, restart deduplication, batch grace/confirmation, missing attribution, file-lock retry, account isolation and read-only HTTP/API boundaries. The delivered HTML was also exercised in Chromium, including sorting, filters, import/export, setup drilldown and live refresh with a synthetic adapter. The analytics checks also cover missing reasons, session boundaries, DST, weekend overlaps and migration from an existing v1-format cache. No connection to your Windows terminal was made while building the kit.
