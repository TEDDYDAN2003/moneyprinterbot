# Dynamic Hull: replay charts, run the demo or sync the journal

**Entry route:** market orders remain the default. For paired candle-relative BUY/SELL limits, use the [direct retest guide](docs/HULL_RETEST.md). Changes and tests do not authorize arming.

**Active Python desk:** [intrabar market entries, confirmed-close fallback, tighten-only cross stops and private-YAML timing](docs/HULL_INTRABAR.md). M5 → M4, M10 → M8, M15 → M12. No M1 trigger; M30/M45 are historical/study frames, not active parents. [Previous M1 release](docs/HULL_M1.md).

**Wickless is a separate bot:** for `WICKLESS OBSERVE`, use the pasted Wickless JSON and the [Wickless operator guide](docs/WICKLESS.md), not Hull profiles, tickets or commands. Ordinary setup, implementation and test requests never send orders.

**Dynamic Hull:** authorize only the selected parents from `[M5, M10, M15]`. Each uses its own fixed trigger and both charts’ closed structure. At most one batch per symbol; same-tick same-side ties go to the highest qualifying parent; opposing eligible pairs skip. A timeframe approval does not authorize arming. `structure_timeframe: null`. A confirmed parent MSS closes only that parent’s verified tickets.

**Hull first-cross rule:** skip an ordinary first aligned cross. Waive the skip only on a confirmed return to the lane's established direction if the intervening counter-MSS leg never made a continuation BOS. Require a fresh aligned cross after the confirmed return close; no time limit, no counter-leg trade prerequisite. No change to immediate MSS exits, sessions, Wickless or chart-study rules.

**Mandatory entry hours (both Python bots):** use the [shared instrument schedule](docs/ENTRY_SESSIONS.md), not the chart statistics buckets or a global killzone. Validate exact broker names; check `entry_sessions.version`, `sha256` and each symbol’s `entry_session` in preflight. A normally closed window is not a reason to widen hours. An unmapped symbol cannot enter. Never disable protection, arm a scanner or change selections to test this filter.

For **MT5 only**, open **PowerShell in the bot folder** on your Windows MT5 PC (the same root used for your current logs; older installs may be named `xauusdm-directional-research`). You should see `config.yaml` and `HullJournal`. No environment activation is needed.

| What do you want to do? | Start here |
|---|---|
| Backtest visually / show open TP | [C. Replay on Free Chart or TradingView](#c-replay-on-free-chart-or-tradingview) — no terminal, credentials, Python or arming |
| See the MT5 chart monitor | [Install the Dynamic indicator](MT5/DYNAMIC-LANES.md) under `MQL5/Indicators` — not an Expert Advisor |
| Send demo orders | [A. Start the demo manually](#a-start-the-demo-manually) — only step 4 sends orders |
| View or export trades | [B. Sync the journal](#b-sync-the-journal) — read-only; no scanner required |
| First use or new code | [Setup](#setup) — do this first, then return here |

## C. Replay on Free Chart or TradingView

1. **Free Chart:** open the symbol on standard **15m** candles → right toolbar **Build Indicators (`</>`) → New Indicator**. Paste [`FC-Dynamic-Hull-Replay.inputs.json`](indicators/freechart/FC-Dynamic-Hull-Replay.inputs.json) in **Inputs** and [`FC-Dynamic-Hull-Replay.js`](indicators/freechart/FC-Dynamic-Hull-Replay.js) in **Script**. Use the full contents, no fences. Name it **FC Dynamic Hull 16 Replay**, set **Overlay on**, choose the symbol’s price precision and **Apply**.
2. **Define a real target.** In its settings enter the **chart tick size**, your **lots and gross USD goal**, the broker’s **USD tick value at one lot**, and the **price size of that broker tick** if different. Recheck per symbol. If no verified cash conversion is available, choose **Chart ticks** and a tick distance instead. Set **Test from UTC** to your replay start if you do not want earlier trades counted. Keep earlier bars loaded for warm-up.
3. **Step replay.** Safe default: the next visible bar’s **open** confirms earlier closes. Use **Completed-bar steps only** only if replay reveals fully closed candles, never for developing bars. On entry, each lane shows a labelled horizontal TP and its **Open TP price**; on settlement that guide disappears. Rates update from confirmed closes. Panning shows the snapshot at the right edge; rewinding recomputes the prefix, not another set of trades.
4. **TradingView:** paste the [Pine indicator](indicators/FC-Hull-Dynamic-Source.pine) in **Pine Editor → Save → Add to chart**. Leave **Display → Show open TP lines (15m / 30m / 45m)** on; the table also prints each held lane’s price. For comparison use the same date window, feed, manual sizing, BE %, **Dynamic/16** and blank structure timeframe.
5. **Interpretation:** these are chart-study trades, **not the Python bot’s or the platform simulator’s fills**. One slot per lane; first aligned cross after MSS allowed; MSS alone does not exit this study. Open slots excluded; unknown TP/BE order stays unknown. Overall and entry-session rates include an *All TFs* count-weighted column. [Worked sizing example, clock choices, troubleshooting and tests](docs/HULL_CHART_REPLAY.md).

## A. Start the demo manually

**1. Check MT5.** Sign in to your **USD hedging DEMO** account. Check the Trade tab for positions and pending orders and [check for another scanner](#check-for-another-scanner). If an armed or unknown scanner is running, or exposure is unresolved, **stop here**. Do not interrupt ticket management or launch a duplicate.

**Saved Journal targets?** The [opt-in Symbol targets panel](HullJournal/START-HERE.md#keep-per-symbol-targets-and-preview-the-spread) and both Windows launchers now honor `hull.enabled` and saved per-symbol bases. The explicit `--profile` examples below override those bases; omit them to use your private settings. Review `specs` in read-only preflight. No step in the Journal arms a scanner.

**2. Choose your symbols.** In this PowerShell window, paste the block below, keeping **only the profiles you intend to run**. These are the eight previously configured profiles. Each line is `SYMBOL:lots per ticket:base USD per ticket:rate`.

```powershell
$py  = '.\.venv\Scripts\python.exe'
$cfg = '.\config.demo.local.yaml'
$profiles = @(
  'XAUUSD:0.10:10:slow'
  'EURUSD:1:10:slow'
  'AUDUSD:1:10:slow'
  'GBPUSD:1:10:slow'
  'GBPJPY:1:10:slow'
  'USDJPY:1.3:10:slow'
  'NAS100USD:1:10:slow'
  'US30:1:10:slow'
) | ForEach-Object { '--profile'; $_ }
if (-not $profiles) { throw 'Choose at least one profile.' }
```

For US30 only, leave only `'US30:1:10:slow'`. **Slow requests ten tickets:** if all fill, that row means **10 lots for the single symbol batch** (subject to account limits), with a **$10 base per ticket**. MT5 values each leg’s entry spread in USD at its own volume. If that value is $13, that leg targets **+$23 gross** before fees, not a guaranteed $23 net. Its ladder uses the same frozen $23. Spread points are not dollars. Verify `US30m` for US30 and `USTEC_x100m` for NAS100USD. Re-run step 2 whenever you open a new scanner window.

**3. Check before sending — read-only.** Use the **same** config and profiles you will arm:

```powershell
& $py -m bot.cli validate-config --config $cfg
if ($LASTEXITCODE -ne 0) { throw 'Invalid config. Do not arm.' }
& $py -m bot.cli hull-preflight --config $cfg --paired-confirm @profiles
if ($LASTEXITCODE -ne 0) { throw 'Preflight failed. Do not arm.' }
```

Verify the demo account, trading permissions, symbols, batch sizes, history on the approved timeframes, positions/orders and `review_before_demo`. Look for `new_ticket_target_policy: base_plus_entry_spread_v1`; each symbol’s `target_preview_by_side` shows the indicative BUY and SELL goals on the current quote. Verify `timeframes: [M5, M10, M15]` (or the selected subset), the correct `parent_to_trigger` mapping, `pair_history_ready: true`, `mss_exit: flatten_own_lane_on_confirmed_shift`, `batch_entry_policy: single_symbol_highest_qualifying_parent`, `entry_policy: intrabar_pair_structure_cross_v1`, `entry_timing: intrabar_cross`, `new_ticket_stop_policy: confirmed_trigger_range_hl_lh_v1`, `stop_spread_policy: sell_ask_entry_only_exact_risk_v2`, and `choch_entry: never_standalone`. Do **not** arm with an unexplained warning.

Want to watch first? Run `& $py -m bot.cli trade-all --config $cfg --paired-confirm @profiles` **without** the arm flag. It must say `READ ONLY`. Stop **that observer** with Ctrl+C before step 4. If the account or profiles change, repeat preflight.

**4. Arm the demo.** Run this command **by itself**, only after reviewing step 3 and deciding to send demo orders:

```powershell
& $py -m bot.cli trade-all --config $cfg --paired-confirm --arm-demo @profiles
```

Confirm `DEMO ARMED` and the intended profiles. Leave this window and MT5 open. Ctrl+C stops the scanner; attached SL/TP stay at the broker, but software exits and ladder updates stop. **Do not stop it with tickets still needing management.**

## B. Sync the journal

**1. Open a second PowerShell in the bot folder.** Keep MT5 on the same hedging demo account. No Algo Trading or `--arm-demo` is needed. Set these variables in **this** window:

```powershell
$py  = '.\.venv\Scripts\python.exe'
$out = '.\HullJournal\data'
```

**Already have a journal?** Set `$out` to the existing folder containing `journal.sqlite3` instead. Keep the checkpoint; run only one writer per folder.

**2. Choose one mode:**

- **Keep syncing + open dashboard:**
  ```powershell
  & $py .\HullJournal\journal.py --repo . --out $out --open-browser
  ```
  Visit **http://127.0.0.1:8765/** on that PC. Wait for **MT5 · live** and a last-sync time. Leave this window open. Opening the HTML file directly shows sample/import mode, not live sync.
- **Sync once + exit** (stop the continuous reporter first):
  ```powershell
  & $py .\HullJournal\journal.py --repo . --out $out --once
  ```

**3. Find your results in `$out`.** `tickets.csv` = fully closed positions; `setups.csv` = completed batches. For a confirmed-stop ticket, compare its cross wick, frozen SL, send quote and actual broker fill; do not assign these fields to older entries without a matched receipt. The first sync defaults to 30 days. Ctrl+C **in the journal window** stops only the reporter, not the bot. For a non-default magic, multiple MT5 installations or older history, see the [journal guide](HullJournal/START-HERE.md).

## Setup

### Check for another scanner

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -match 'bot\.cli\s+(trade-all|trade|demo)\b' } |
  Select-Object ProcessId,CommandLine
```

**First use / after an update:** with no affected running scanner or unresolved exposure, run `git status --short --branch`. For a published update on clean `main`, run `git pull --ff-only origin main`. For an offline Git update use the supplied verified fast-forward bundle. A portable ZIP has no Git history: extract to a new folder; never overwrite private files. If the pull fails or the checkout is dirty, stop; never force or discard local work.

1. **Prepare Python.** Use supported 64-bit Windows Python 3.11+. Reuse `.venv` if present:
   ```powershell
   if (-not (Test-Path .\.venv\Scripts\python.exe)) { py -3 -m venv .venv }
   & .\.venv\Scripts\python.exe -m pip install -e ".[test]" MetaTrader5 tzdata
   if ($LASTEXITCODE -ne 0) { throw 'Dependency install failed.' }
   & .\.venv\Scripts\python.exe -m pytest -q
   if ($LASTEXITCODE -ne 0) { throw 'Tests failed. Do not arm.' }
   ```
   **Journal only?** You can go to [B](#b-sync-the-journal) now.
2. **Prepare the demo config.** Create it only if missing; never overwrite a private config:
   ```powershell
   if (-not (Test-Path .\config.demo.local.yaml)) { Copy-Item .\config.yaml .\config.demo.local.yaml }
   notepad .\config.demo.local.yaml
   ```
   In its **existing `run:` section**, set `mode: demo`. In `hull` set `execution_mode: paired_structure`, `entry_timing: intrabar_cross` and verify `eligible_timeframes: [M5, M10, M15]`; in `hull.indicator` set `stop: cross_candle` and verify `hull_len: 16`, `source: dynamic`, `structure_timeframe: null`, `exit_opposite: true` and `exit_on_mss: true`. Save the private copy; do not guess the timeframe of legacy tickets or erase state. Leave the tracked `config.yaml` in `paper` mode. Go to [A](#a-start-the-demo-manually).

## When does the signal appear?

By default the fresh Hull cross can enter on a **forming M4, M8 or M12 candle** when the last closed structures of the trigger and its own parent agree. The original candle’s later close can tighten protection or request an exit; the opening order already carries SL/TP. Separately confirmed fresh closes are a fallback, not a second batch. Explicit closed timing and direct retest wait for confirmation. A confirmed parent MSS remains an exit for its own lane. [Timing and stops](docs/HULL_INTRABAR.md).

## Desktop agent rules

A question about this guide is not authorization to trade. A normal start request means **observer only**. Follow the [standing startup rules](README.md#standing-instructions-for-the-desktop-agent). An agent must receive separate, explicit **`ARM DEMO`** for the verified profiles before using step A4. No live-money mode.

A pull does not update a running process. Match its `started.summary.checkout_head_at_start` to `git rev-parse HEAD`; verify the source, the effective policy (`intrabar_pair_structure_cross_v1` for the default market route), intended Hull settings (Dynamic, `indicator.hull_len: 16`), quote policy and per-ticket ladder preview. Never change lots, limits or stops to force a trade, or delete safety state. Logs: `reports/hull_runtime/<account-hash>/shadow.jsonl` (observer) and `demo.jsonl` (armed). [Exposure, funding and retired limits](docs/HULL_DEMO.md).
