# Moneyprinterbot - Hull account and demo commands

Run these commands from the repository root in PowerShell.

```powershell
Set-Location -LiteralPath 'C:\Users\hp\Downloads\xauusdm-directional-research'
.\.venv\Scripts\Activate.ps1

$python = Join-Path $PWD '.venv\Scripts\python.exe'
$profiles = @(
  '--profile','XAUUSD:0.10:10:slow',
  '--profile','EURUSD:1:10:slow',
  '--profile','AUDUSD:1:10:slow',
  '--profile','GBPUSD:1:10:slow',
  '--profile','GBPJPY:1:10:slow',
  '--profile','USDJPY:1.3:10:slow',
  '--profile','NAS100USD:1:10:slow',
  '--profile','US30:1:10:slow'
)
```

The broker aliases are `USTEC_x100m` for `NAS100USD` and `US30m` for `US30`. Slow profiles request 10 tickets per symbol with $10 target per ticket.

## Verify the checkout and environment

```powershell
git status --short --branch
if ((git branch --show-current) -ne 'main') { throw 'Stop: use a clean main checkout.' }
if (git status --porcelain) { throw 'Stop: preserve local changes and make the checkout clean before updating.' }
Get-CimInstance Win32_Process -Filter "name='python.exe' OR name='pythonw.exe'" |
  Where-Object { $_.CommandLine -match 'bot\.cli\s+(trade-all|trade|demo)\b' } |
  Select-Object ProcessId,CommandLine
```

Inspect the process list and open exposure **before** the next block. Do not update over an armed or unknown scanner, interrupt ticket management, or start duplicates. For a safe, clean checkout:

```powershell
git pull --ff-only origin main
if ($LASTEXITCODE -ne 0) { throw 'Stop: fast-forward pull failed.' }
& $python -m pip install -e ".[test]" MetaTrader5 tzdata
if ($LASTEXITCODE -ne 0) { throw 'Stop: dependency installation failed.' }
& $python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw 'Stop: tests failed.' }
& $python -m bot.cli validate-config
if ($LASTEXITCODE -ne 0) { throw 'Stop: configuration validation failed.' }
```

Stop if the pull fails, the checkout is dirty, tests fail or the configuration is invalid. Do not reset, force, checkout over local work or discard uncommitted changes.

Before reusing or launching a running scanner, compare its startup checkout and policy IDs with the current `git rev-parse HEAD`. Never update over an armed process or open duplicate scanners.

For the current Hull release, check preflight and the running `started.summary` for `entry_policy: protected_swing_v2`, `signal_policy: fc_hull_either_v2`, `indicator.source: either`, `indicator.hull_len: 16`, and `batch_entry_policy: first_eligible_while_flat_v1`. Do not auto-edit a private profile: to adopt v2 on an older full local YAML, the operator changes just `hull.indicator.source` and `hull.indicator.hull_len`. A second BUY is ignored while any BUY ticket remains; a later fresh BUY after flat may qualify in the same zone (SELL mirrors BUY). Keep initial protection, the existing opposite-label exit and the 70/30 ladder. [Detailed behavior](docs/HULL_DEMO.md#what-triggers-an-entry).

## Read-only demo preflight

```powershell
& $python -m bot.cli hull-preflight --config config.yaml @profiles
```

Preflight must report a USD hedging DEMO account, enabled terminal/expert trading permissions, valid symbol resolutions and no unowned pending exposure unless manually reconciled.

Do not treat preflight success as authorization to send orders.

Read the full report before starting. Confirm the account, resolved symbols, M1/M2/M5 history, permissions, open exposure, limits and `review_before_demo`. The eight slow profiles can request up to 10 tickets each; the 1.0-lot FX profiles mean 10 lots per symbol, not one lot total.

## Read-only demo observer

```powershell
& $python -m bot.cli trade-all --config config.yaml @profiles
```

This observes M1, M2 and M5 and sends no orders. It waits for fresh completed-candle labels and never migrates existing tickets.

## Armed demo account

The ordinary start command above is a read-only observer. It does not send orders. Only after the operator gives the separate explicit instruction `ARM DEMO` for the exact verified profiles, run the preflight below using a local `config.demo.local.yaml` with `run.mode: demo`:

```powershell
& $python -m bot.cli hull-preflight --config config.demo.local.yaml @profiles
```

A pull does not rewrite an existing `config.demo.local.yaml`. Under **`hull.limits` only**, the defaults are `max_positions: null`, `max_total_lots: null`, `max_open_risk_usd: null` and `min_free_margin_pct: 0`. Preflight lists any enabled overrides in `review_before_demo`. To adopt the new defaults in an existing file, change or remove just those four keys; keep your other settings. See [how funding and vetoes work](docs/HULL_DEMO.md#hull-portfolio-limits-and-broker-funding).

Check `retired_limits`: it must list `drawdown_usd` and `max_orders_minute`. The Hull scanner ignores those two keys in older YAML and ignores any old drawdown halt/send history in the journal. No journal edit or local YAML edit is needed for **these two removals**. The bot can keep opening funded batches after losses; broker rejections and ticket reconciliation still apply.

Review that report again, including the account, all symbol specs, effective limits and `review_before_demo`. Only if those checks pass, start the armed scanner in a separate command:

```powershell
& $python -m bot.cli trade-all --config config.demo.local.yaml --arm-demo @profiles
```

The local demo config must set `run.mode: demo` and define every key in `$profiles` under `hull.symbols`; this ignored file can lag `config.yaml`, causing `Unknown or repeated symbol key`. Copy only the missing Hull symbol entries from `config.yaml` while preserving terminal, account and other local settings. Validate and preflight with the exact profiles before arming. The scanner refuses live, contest and netting accounts. Never reuse stale arming approval for changed profiles, resize lots or start a second scanner for the same account.

For ladder-enabled profiles, report preflight's per-ticket ladder preview rather than replacing it with an assumed total.

## Future real-account starts

There is currently no supported live-account or real-order command in this repository. Do not use `--arm-demo`, `--arm-orders`, config edits or alternate flags to attempt live trading.

A real-account start requires a separate authorization and live-specific margin, loss, reconciliation and kill-switch implementation.

## Stop and runtime logs

Press `Ctrl+C` in the scanner terminal. Broker-attached SL/TP remains on existing positions after software exit.

```text
Demo: reports/hull_runtime/<account-hash>/demo.jsonl
Observer: reports/hull_runtime/<account-hash>/shadow.jsonl
```
