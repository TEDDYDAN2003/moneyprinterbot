# Entry hours · Dynamic Hull and Wickless M15.

Both Python bots use **one mandatory file**: [`src/bot/config/entry_sessions.yaml`](../src/bot/config/entry_sessions.yaml). It filters **new entries** on instruments you already selected. It does not select symbols, change lots, create extra entries, or tell either bot to flatten at the end of a window.

## Which market can enter when?

| Market | Allowed entry windows (any listed window is enough) |
|---|---|
| JP225 | **09:00–10:00 Tokyo** |
| DE40 | **09:00–10:00 Berlin** (Frankfurt cash open) |
| USDJPY | **09:00–10:00 Tokyo**, London before NY, the full overlap, or the New York day |
| GBPJPY, XAUUSD, EURUSD, AUDUSD, GBPUSD | **London before NY**, the **full overlap**, or the **New York day (08:00–17:00 New York)** |
| NAS100USD, US30, US500 | **13:00–16:00 London while both cities are open**, **09:30–10:30 New York**, or the **New York day**. Overlapping windows never multiply entries. |

**Full overlap** means London **08:00–17:00 Europe/London** *and* New York **08:00–17:00 America/New_York**. The opening instant is included; the ending instant is excluded. Windows use **local weekdays** and IANA time zones. They are not exchange holiday or half-day calendars. A scheduled window is permission to consider a signal, not evidence that the broker is open or that a fill is possible.

### Nairobi quick reference (EAT, UTC+3)

| Window | UK/Europe and US on standard time | Both on summer time | US on summer time, Europe on standard time |
|---|---|---|---|
| Tokyo first hour | 03:00–04:00 | 03:00–04:00 | 03:00–04:00 |
| Frankfurt first hour | 11:00–12:00 | 10:00–11:00 | 11:00–12:00 |
| London before NY | 11:00–16:00 | 10:00–15:00 | 11:00–15:00 |
| Full overlap | 16:00–20:00 | 15:00–19:00 | 15:00–20:00 |
| Index overlap killzone | 16:00–19:00 | 15:00–18:00 | 16:00–19:00 |
| Full New York day | 16:00–01:00 next day | 15:00–00:00 | 15:00–00:00 |
| NY cash first hour | 17:30–18:30 | 16:30–17:30 | 16:30–17:30 |

For example, on **Tuesday 27 October 2026 at 15:00 EAT**, New York has just opened its 08:00 session and London is at noon. AUDUSD can enter the full overlap. US30 can already consider an entry under the New York day window. Its narrower killzone starts at **16:00 EAT**. At **16:30 EAT** all three index windows match, still just one admission decision per signal.

## What each bot checks

- **Dynamic Hull:** check the **completed cross candle’s close time**. A forming cross is exit-only, never an entry-session trigger. A fresh post-signal quote and the actual decision time must also qualify. The same rule applies on any first-quote wait, reprice, next batch leg and final send. If a window ends after a fill, keep the filled tickets protected; do not top up the remainder. Market structure and the first-cross-after-MSS rule still advance out of hours. A raw opposite cross, lane MSS exit, KILL, SL/TP alignment and ladder management are **not** subject to entry hours.
- **Wickless (No Wick):** track M15 setups, retests, cancellations and trend at all hours. Test the **rejection candle’s close**, not its open and not the original setup time. A setup formed before the window can still qualify if it rejects inside it. The fresh post-close quote, submission decision and last check after reserving the intent must be in-window. An out-of-hours rejection is consumed, never queued for the next opening. Broker protection and the exact-ticket TP correction after slippage are unchanged.
- Observer plans follow the same schedule as guarded demo entries. All existing quote-age, broker, spread, funding, risk and ownership checks still apply. Hull's configured maintenance/rollover and optional global restrictions can **further restrict**, never expand, the symbol's window. Neither `trade_only_in: []` nor `session: continuous` disables the shared policy.

## Verify without starting a scanner

From the updated installed checkout (no MT5 required):

```powershell
python -m bot.config.entry_sessions
python -m bot.config.entry_sessions --at 2026-10-27T15:00:00+03:00 --symbol AUDUSDm --symbol US30m
```

The first command shows all 11 markets as of now; the second checks a specific instant. No naive machine-local timestamps. Look for the resolved **market**, assigned **windows**, **active** matches, UTC evaluation time, and **status**. An ordinary `outside_symbol_session` means the map is valid but closed. `entry_session_unmapped_symbol` or `entry_session_profile_mismatch` means no opening permission; verify the exact broker name and Hull profile. For example, `USTEC_x100m → NAS100USD`, `DE30m → DE40`, `JP225m → JP225`. No suffix stripping, substring guessing, or default hours for synthetics (`1HZ100V` has no schedule).

`python -m bot.cli validate-config` checks the enabled Hull keys / pinned names offline. `hull-preflight` checks the **actual** resolved names for the requested profiles under `specs[broker_symbol].entry_session`. `python -m bot.wickless validate --config wickless.example.json` checks the exported pairs; Wickless preflight logs each `symbol_check.entry_session`. Normal closed hours are not a mapping error; other preflight checks may still fail. Never arm on an unexplained warning.

Startup/validation identifies the policy **file, version and SHA-256**. Compare the two runners' hashes if they use separate installs. Hull logs the trigger window in `label_decision.signal_session` and the submission window in `demo_result.entry_session`. `entry_session_veto` identifies the failed stage and clock (signal, quote or decision), and the number of legs already sent. Wickless logs the rejection and applicable decision/veto in `entry_decision`. No chart changes are needed: TradingView, Free Chart, the journal and the offline reference replays remain separate from execution admission.

**Updating:** The packaged file loads from the installed `bot.config` module at startup, not the current directory, Hull private YAML or Wickless JSON. In an editable checkout, edit only the shared YAML if an explicitly verified broker alias is needed. Do not assign an unresearched symbol to another market simply to pass validation. With a wheel, rebuild/reinstall to deliver a file change. A missing or invalid file blocks new entries; it does not remove broker stops or disable Hull's management of already-owned tickets. A running process keeps its startup policy. Update/restart safely when the account is flat and no tickets need software management; preserve the private configs and state.
