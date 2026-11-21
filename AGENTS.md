# Hull: start the demo or sync the journal

**Wickless is a separate bot:** for `WICKLESS OBSERVE`, use the pasted Wickless JSON and the [Wickless operator guide](docs/WICKLESS.md), not Hull profiles, tickets or commands. Ordinary setup, implementation and test requests never send orders.

**Dynamic Hull upgrade:** no timeframe is eligible until the operator names it. `hull.eligible_timeframes: []` is intentional. A profile, CLI override or old chart preset cannot authorize a lane. Do not start an observer or arm until the list is provided. [Policy and private-config migration](docs/HULL_STRUCTURE.md).

Open **PowerShell in the bot folder** on your Windows MT5 PC (usually `$HOME\Downloads\xauusdm-directional-research`). You should see `config.yaml` and `HullJournal`. No environment activation is needed.

| What do you want to do? | Start here |
|---|---|
| Send demo orders | [A. Start the demo manually](#a-start-the-demo-manually) — only step 4 sends orders |
| View or export trades | [B. Sync the journal](#b-sync-the-journal) — read-only; no scanner required |
| First use or new code | [Setup](#setup) — do this first, then return here |

## A. Start the demo manually

**1. Check MT5.** Sign in to your **USD hedging DEMO** account. Check the Trade tab for positions and pending orders and [check for another scanner](#check-for-another-scanner). If an armed or unknown scanner is running, or exposure is unresolved, **stop here**. Do not interrupt ticket management or launch a duplicate.

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

For US30 only, leave only `'US30:1:10:slow'`. **Slow requests ten tickets:** if all fill, that row means **10 lots total**, with a **$10 base per ticket**. MT5 values each leg’s entry spread in USD at its own volume. If that value is $13, that leg targets **+$23 gross** before fees, not a guaranteed $23 net. Its ladder uses the same frozen $23. Spread points are not dollars. Verify `US30m` for US30 and `USTEC_x100m` for NAS100USD. Re-run step 2 whenever you open a new scanner window.

**3. Check before sending — read-only.** Use the **same** config and profiles you will arm:

```powershell
& $py -m bot.cli validate-config --config $cfg
if ($LASTEXITCODE -ne 0) { throw 'Invalid config. Do not arm.' }
& $py -m bot.cli hull-preflight --config $cfg @profiles
if ($LASTEXITCODE -ne 0) { throw 'Preflight failed. Do not arm.' }
```

Verify the demo account, trading permissions, symbols, batch sizes, history on the approved timeframes, positions/orders and `review_before_demo`. Look for `new_ticket_target_policy: base_plus_entry_spread_v1`; each symbol’s `target_preview_by_side` shows the indicative BUY and SELL goals on the current quote. Verify `entry_policy: fc_dynamic_skip_first_mss_v2`, `entry_timing: live_hull_cross`, and `choch_entry: never_standalone`. Do **not** arm with an unexplained warning.

Want to watch first? Run `& $py -m bot.cli trade-all --config $cfg @profiles` **without** the arm flag. It must say `READ ONLY`. Stop **that observer** with Ctrl+C before step 4. If the account or profiles change, repeat preflight.

**4. Arm the demo.** Run this command **by itself**, only after reviewing step 3 and deciding to send demo orders:

```powershell
& $py -m bot.cli trade-all --config $cfg --arm-demo @profiles
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

**3. Find your results in `$out`.** `tickets.csv` = fully closed positions; `setups.csv` = completed batches. The first sync defaults to 30 days. Ctrl+C **in the journal window** stops only the reporter, not the bot. For a non-default magic, multiple MT5 installations or older history, see the [journal guide](HullJournal/START-HERE.md).

## Setup

### Check for another scanner

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -match 'bot\.cli\s+(trade-all|trade|demo)\b' } |
  Select-Object ProcessId,CommandLine
```

**First use / after an update:** with no affected running scanner or unresolved exposure, run `git status --short --branch`. Only on clean `main`, run `git pull --ff-only origin main`. If the pull fails or the checkout is dirty, stop; never force or discard local work.

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
   In its **existing `run:` section**, set `mode: demo`, then save. Leave the tracked `config.yaml` in `paper` mode. Go to [A](#a-start-the-demo-manually).

## When does the signal appear?

On an eligible lane, a fresh raw cross can appear on a forming candle. Structure changes only on a confirmed structure close. Bullish BUY / bearish SELL may enter if flat and protected; a raw opposite cross exits the owned opposing side regardless of entry eligibility. A structure event alone is not a trade. [Exact rules](docs/HULL_STRUCTURE.md).

## Desktop agent rules

A question about this guide is not authorization to trade. A normal start request means **observer only**. Follow the [standing startup rules](README.md#standing-instructions-for-the-desktop-agent). An agent must receive separate, explicit **`ARM DEMO`** for the verified profiles before using step A4. No live-money mode.

A pull does not update a running process. Match its `started.summary.checkout_head_at_start` to `git rev-parse HEAD`; verify the source, `fc_dynamic_skip_first_mss_v2`, intended Hull settings (default Dynamic/24), quote policy and per-ticket ladder preview. Never change lots, limits or stops to force a trade, or delete safety state. Logs: `reports/hull_runtime/<account-hash>/shadow.jsonl` (observer) and `demo.jsonl` (armed). [Exposure, funding and retired limits](docs/HULL_DEMO.md).
