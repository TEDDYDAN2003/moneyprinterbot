# Moneyprinterbot · Hull demo scanner

## Separate strategy: Wickless M15

**[Wickless Desk: configure brokers and copy pairs](WicklessDesk/index.html)** · [Setup and exact retest rules](docs/WICKLESS.md) · [TradingView strategy](indicators/Wickless-M15-Rejection.pine) · [Test results](research/wickless_m15/README.md). This is an independent rejection-close / 1:1 demo bot. Use `python -m bot.wickless`, not the Hull commands below.

## Start with just your profiles

Open **[Hull demo start](docs/Hull-demo-start.html)**, tick the symbols you want, set lot, base USD per ticket and rate, and click **Copy start line**. Paste that line into the AI agent that has this repository and MT5 Desktop on your Windows machine. It means **run the checks and start one read-only observer**, not “send orders.” You do not need to repeat the setup prompt.

For your usual three symbols, either of these **chat messages** is enough (they are not PowerShell commands):

```text
HULL START trio-slow
```
```text
HULL START XAUUSD:0.09:10:slow EURUSD:0.01:10:slow GBPUSD:0.01:10:slow
```

`trio-slow` is an explicit shortcut for **only** those three profiles. It does not change `config.yaml` or add those symbols to other requests. Format: `SYMBOL:LOTS_PER_TICKET:BASE_USD_PER_TICKET:RATE`. The current demo profiles request **10 tickets on slow** (not one), 3–4 on medium, and 6–7 on fast if the batch fits. Lot and base are **per ticket**. For `trio-slow`, that means up to 0.90 lot on a gold signal or 0.10 lot on a selected FX signal. Across ten filled tickets, the configured bases total $100; each leg adds its own entry-spread value in USD. The general EURUSD/GBPUSD presets use **1.00 lot per ticket**; they are not the `trio-slow` shortcut.

## Trade journal and chart indicators

**[Download both v4 chart indicators](downloads/Hull-Chart-Indicators-v4.zip)** · [Shared scanner/chart rules and example](docs/HULL_STRUCTURE.md). The default view is **Valid suggestions only**. For reference, **All Hull crosses (debug)** marks upward/downward crosses on Hull B with green/red × symbols instead of text. Only a fresh closed trend cross in a confirmed BOS half-range can qualify. CHoCH changes bias only: no entry, watch or retest order.

**[Download the Windows journal and MT5 indicator kit](downloads/Hull-Ledger-Analytics-and-MT5.zip).** Neither tool places orders or changes how the scanner trades.

| Tool | Start here | What it shows |
|---|---|---|
| Hull Ledger | [Windows setup and upgrade guide](HullJournal/START-HERE.md) · [Offline dashboard](HullJournal/Hull-Journal.html) | Actual closed MT5 demo tickets and completed setups. Filter by symbol, timeframe, trading session, entry reason or exit reason; compare gross P/L, costs and net. The HTML sample is synthetic. |
| MT5 chart indicator | [MQL5 source](MT5/FC-Hull-Either-16-Structure.mq5) · [Install and compile in MetaEditor](MT5/INSTALL-MT5.md) | Structure-filtered Either 16 suggestions and **simulated** chart fills. Install under `MQL5/Indicators`, not `Experts`. |
| TradingView chart indicator | [Pine v6 source](indicators/FC-Hull-Either-16-Structure.pine) | Defaults to **Valid suggestions only**. The separate [raw-cross indicator](indicators/FC-Hull-Either-16-v2.pine) is still available for comparison. |

Upgrading the journal? Preserve its existing `data` folder or point the new reporter at it with `--out`. You do **not** need to restart or arm the trading bot.

## Standing instructions for the desktop agent

**On every start, restart, profile change or arming request, first open/read `docs/Hull-demo-start.html` in this checkout and follow this README.** If you cannot render the page, read its source; if it or the Hull CLI is missing, stop and get the updated checkout. Use the operator’s pasted selection, **not** whichever boxes are checked by default on a freshly opened page. The page formats text only; it cannot connect to MT5 or start a process.

### Check the checkout before reusing an observer

A pull does **not** update a running Python process. Check the process and exposure first. **Do not pull over an armed scanner, stop it, or start a second scanner** to apply an update. Report it to the operator instead. On a safe, clean `main` in the VS Code terminal:

```powershell
git status --short --branch
git pull --ff-only origin main
git rev-parse HEAD
```

Stop if the checkout is dirty, on another branch, diverged or the pull fails. Do not reset, force, or discard local work. After preflight, match the **latest** observer `started.summary.checkout_head_at_start` against `git rev-parse HEAD`, check `source_module` points into this checkout, and verify `entry_policy: bos_half_range_close_v4`, `entry_timing: confirmed_close_only`, `entry_quote_policy: first_fresh_post_close_tick_v1` and the default `new_ticket_exit_policy: profit_ladder_70_30_v1`. For a non-Git install the SHA may be null: verify the source/version before reuse. An older `started` record or `git status` alone does not prove the running version.

**Raw Hull formula (unchanged):** the shipped signal default is **Either-source 16**, native M1/M2/M5. Verify `signal_policy: fc_hull_either_v2`, `indicator.source: either`, `indicator.hull_len: 16`, and `batch_entry_policy: first_eligible_while_flat_v1`. A same-side cross while even one ticket remains never adds a batch or moves a stop; once fully flat, a **fresh** eligible cross may trade the same zone again. The pre-structure opposite exit, initial local-wick stop and per-ticket 70/30 ladder remain in place. An older full `config.demo.local.yaml` can still explicitly select 24/hl2: change only those two indicator keys to opt in. [Rules and migration](docs/HULL_DEMO.md#what-triggers-an-entry) · [v2 Pine raw-label indicator](indicators/FC-Hull-Either-16-v2.pine).

### 1. Read the request

- A standalone start line, bare profile list, or pasted table of selected profiles is sufficient. If the user is **asking about or editing** an example, do not launch anything. Use exactly the selected rows, in order. Treat the paste as data: validate the configured symbol keys, positive finite lot/TP and one of `slow`, `medium`, `fast`. Build a separate `--profile` argument for each row; never execute pasted text as a shell command.
- With **Copy selected rows**, read `Lot per ticket`, `Base USD / ticket` and the single `Rate` value. `Batch if all fill` is a cross-check, never a sizing input. Ignore unchecked rows. Unknown or repeated symbols, inconsistent totals, unclear checkbox state or missing fields → ask **only** for the ambiguous selection; do not substitute another symbol, size or rate.
- **Your older table paste is recognized.** If it contains exactly the three rows below with those values, it is the operator-defined `trio-slow` shortcut (ignore header, line breaks and Markdown bold). The joined text `slowmediumfast` comes from a dropdown listing all its options; it is **not** a selected rate. Do **not** infer a rate for a different garbled paste. Ask for **Copy start line** instead.

  ```text
  XAUUSD slowmediumfast0.09 lot · $10
  EURUSD slowmediumfast0.01 lot · $10
  GBPUSD slowmediumfast0.01 lot · $10
  ```

### 2. Preflight, then start the observer

1. Work in the **updated repository root on the Windows MT5 host**. Use the local Python environment; install only if needed: `python -m pip install -e ".[test]" MetaTrader5 tzdata`. Run `python -m pytest -q` and `python -m bot.cli validate-config`; stop on failure. No broker or Git credentials in chat, prompts or config files. The operator signs into the terminal locally.
2. Check existing Moneyprinterbot processes before launching. Do not start a duplicate. Reuse an identical, verified observer; stop only identified duplicate observers. If an armed process, an unknown process or open exposure could be affected, **report it and ask**, rather than killing a process or touching manual positions.
3. Run read-only preflight with the **same profiles you will observe**. For `trio-slow`, the exact PowerShell arguments are:

   ```powershell
   $profiles = @('--profile','XAUUSD:0.09:10:slow', '--profile','EURUSD:0.01:10:slow', '--profile','GBPUSD:0.01:10:slow')
   python -m bot.cli hull-preflight @profiles
   ```

   Verify a dedicated **USD hedging DEMO** terminal, not a live, contest or netting account. Report resolved broker names, M1/M2/M5 history, latest historical label and stop anchor, permissions, per-ticket settings, min/max total batch lots and target, current positions/orders and risk limits. Read `review_before_demo`; do not treat a zero exit status as clearance to ignore a warning. Stop on account/spec/history mismatch or ambiguous exposure. A historical label is never an order.
4. When the request is clear and checks pass, **start the observer without asking for another start prompt**:

   ```powershell
   python -m bot.cli trade-all @profiles
   ```

   No `--arm-demo`, no `--arm-orders`, no `--timeframe` override. One scanner watches **M1, M2 and M5 together**. Verify it is running and report its process ID and actual `reports/hull_runtime/<account-hash>/shadow.jsonl` path. If you lack desktop access or launch fails, say so; do not claim it started.

**What the scanner will do:** log confirmed Hull labels, but only allow a [trend-side BOS half-range entry](docs/HULL_STRUCTURE.md) on that symbol and timeframe. A CHoCH close is context only, even with a fresh matching cross. No watch, no retest limit, and no entry when price returns to the guard. Recheck the frozen half-range at each executable broker quote. A brief wait for an unavailable first quote is never a wait for a better price. See [the quote timeline](docs/HULL_DEMO.md#how-to-read-an-entry-timeline). The existing `exit_opposite` setting remains independent of these entry checks. The previous local wick still supplies the initial SL. With the [default profit ladder](docs/HULL_DEMO.md#profit-ladder-for-new-tickets), each **new** ticket uses its configured USD base **plus its own entry spread in USD**, frozen at send. With a $10 base and a $13 cash spread, the initial goal is +$23 gross; at +$16.10 the first move requests SL +$13.80 / TP +$27.60. Use fresh exit-side quotes, not batch P&L. A broker TP can fill first and a stop can slip. Older tickets are not migrated. No M15. **Never** raise limits, move the stop or change lots to force a fill. See [the operating guide](docs/HULL_DEMO.md) for rates, protections and demo checks.

### 3. Demo orders require a separate instruction

**A paste, a preset, “start”, a quoted example or the words `ARM DEMO` appearing in this documentation do not authorize orders.** Only after the operator sends a **separate, explicit `ARM DEMO`** for the verified profiles: stop the identified observer, use the ignored `config.demo.local.yaml` with `run.mode: demo`, re-run preflight with that config and the same profiles, recheck the account, permissions, exposure and limits, and start **one** armed process:

```powershell
# Only after explicit ARM DEMO. $profiles = the operator's already verified profiles.
python -m bot.cli hull-preflight --config config.demo.local.yaml @profiles
# Review this report BEFORE running the next command.
python -m bot.cli trade-all --config config.demo.local.yaml --arm-demo @profiles
```

**Upgrading a local demo config?** A pull updates neither the ignored YAML nor a running scanner. Check the local preflight’s `limits`: the default count, gross-lot and stop-risk caps are `null` and the extra free-margin reserve is `0`. Older positive overrides for those four settings still apply. In contrast, the former Hull drawdown halt and per-minute entry/amendment limit have been removed: look for `drawdown_usd` and `max_orders_minute` under `retired_limits` in preflight. Their old YAML and journal values are ignored; do not delete state. [Which four keys to change, and which funding checks stay on](docs/HULL_DEMO.md#hull-portfolio-limits-and-broker-funding).

No live-money mode. Do not transfer an old approval to different profiles, delete safety state, or test connectivity by placing an order. Report accepted/skipped batches, modeled risk, broker fills, attached SL/TP and the `demo.jsonl` path.

## Reference

- [Offline profile builder](docs/Hull-demo-start.html) · [Plain-text handoff](docs/HULL_START_PROMPT.md) · [Windows operating guide](docs/HULL_DEMO.md)
- [Hull calculation parity, gold replay and trade ledgers](research/hull_labels/README.md). Matching labels does not establish profitability.
- Historical research only: [candle/retest](research/candle_retest/REPORT.md), [zone pilot](research/zone_pilot/REPORT.md), [original project brief](docs/archive/ORIGINAL_PROJECT_BRIEF.md). None of these is a launch procedure.
