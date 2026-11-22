# Hull Ledger | Analytics edition · Windows quick start

A read-only journal for the Hull bot’s **actual MT5 hedging-demo fills**. It does not send orders, subscribe to TradingView, change the scanner, or require a cloud/database account. A BUY arrow, shadow plan or attempted order is never a trade in this ledger.

## Upgrade an existing journal without losing history

Stop **only the journal reporter** in its own window. Keep a backup of your entire existing `data` folder, including the SQLite checkpoint. Do not delete the bot’s state, restart the scanner, or reset your CSVs. The kit contains **no** live `data` directory.

Either replace the reporter program files in the same folder, or extract the new kit separately and launch **its** `journal.py` with `--out` pointing at the previous `data` directory. Keep the same `--repo`, magic and terminal selection. The first successful poll reuses the checkpoint and regenerates the extra CSV columns. The optional account-scoped audit log is re-indexed once to discover past exit-request notes. The new HTML can also import the old ticket/setup CSVs directly; download a filtered copy if you want enriched columns without running MT5. Use the local dashboard URL (refresh your tab), not a previously downloaded old HTML file.

## Start on the MT5 computer

1. Extract the **entire ZIP** to a writeable folder outside the running bot’s checkout, e.g. `Downloads\HullJournal`. Keep `journal.py`, `ledger.py`, `hull_analytics.py` and `Hull-Journal.html` together. Open MT5 Desktop and sign in to the intended **hedging demo** account. You do not need to enable Algo Trading, arm or restart the bot for this reporter.
2. Double-click **`Start-Journal.cmd`**. It uses the existing bot Python environment if it finds `%USERPROFILE%\Downloads\xauusdm-directional-research\.venv\Scripts\python.exe`. Otherwise it tries the Windows Python launcher or `python`. It installs nothing. You may also drag the bot folder onto the launcher to supply a different path.
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

## Files and timing

| File | What it means |
|---|---|
| `data/tickets.csv` | One row for each **fully closed** Hull position, keyed by the stable MT5 position identifier, not by an individual fill. Partial exits wait for the last exit. |
| `data/setups.csv` | One row per **completed signal batch** (account, broker symbol, timeframe, exact trigger and direction). It sums the actually filled tickets, not the planned count. It waits for open or unresolved siblings. |
| `data/journal.sqlite3` | Local private checkpoint, linked deal history and exact audit metadata. It prevents duplicate rows after restarts. Do not edit the database or the bot’s state. |
| `Hull-Journal.html` | Open directly for a **labelled synthetic preview** or manual CSV import; use the local HTTP address above for automatic live updates. |
| `examples/` | Synthetic, explicitly marked `SAMPLE` CSVs. Never mixed into real exports. |

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
- **Open price / close price** are volume-weighted fill prices. Times are the first opening and last liquidation deal. Setup prices are lot-weighted across its same-symbol, same-side positions. `initial_sl` and `initial_tp`, when present, are the **initial requested** values from the matched bot audit, not a claim about final ladder levels. `exit_reason` uses the last closing deal; `Mixed exits` identifies a setup with different ticket exit reasons.
- **Timing in ticket details:** `confirmed_close` uses the signal candle’s close. in **historical execution logs**, `live_hull_cross` shows the broker trigger tick **before** the scheduled candle end. `signal_trigger_utc` is not the fill time (`open_time_utc`); `signal_close_utc` is deliberately blank for a live trade. The trigger bid/ask and native bar boundaries come from the matched log, not estimated from chart graphics. Without that log the live comment still supplies the exact trigger milliseconds, and missing details stay blank. Older CSVs remain importable.
- Timeframe, signal time and leg come from a Hull entry comment such as `h2_<signal-close-seconds>_3` (closed label) or `i2_<trigger-milliseconds>_3` (live cross), or the original request on an **exact entry deal-ID match** in the account’s real demo log. Current v4 records `bull_bos_discount_buy` and `bear_bos_premium_sell` on closed crosses only. No CHoCH or retest entry exists in v4. On older records, v2 `closed_label` reasons distinguish a CHoCH-close entry, a pullback above/below a protected swing, and the existing post-BOS countertrend entry. Historical `bull_discount_buy` / `bear_premium_sell` labels keep their original meanings; they are never relabelled as v2 trades. A missing/contradictory tag means **Unknown** and no guessed setup. A missed or rejected signal has no fill, therefore no P/L. Earlier bot versions using the same magic may be included; use date filters for the relevant rollout.
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

Use the same `--repo`, `--out`, `--magic` and terminal selection as your normal run. The local service binds only to loopback and exposes just the dashboard and its snapshot (no account login, directory listing or writable API). The private files are not encrypted: protect the Windows folder. For a consistent backup, stop the reporter and copy the whole `data` folder. Do not publish private CSVs or the cache by accident.

## Verification, no MT5 needed

```powershell
py -3 -m unittest discover -s tests -v
```

The kit includes deterministic tests covering partial/manual exits, cost accounting, long-position backfill, late corrections, restart deduplication, batch grace/confirmation, missing attribution, file-lock retry, account isolation and read-only HTTP/API boundaries. The delivered HTML was also exercised in Chromium, including sorting, filters, import/export, setup drilldown and live refresh with a synthetic adapter. The analytics checks also cover missing reasons, session boundaries, DST, weekend overlaps and migration from an existing v1-format cache. No connection to your Windows terminal was made while building the kit.
