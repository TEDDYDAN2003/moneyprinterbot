# Dynamic Hull: signal, entry and exit

The scanner’s signal reference is the operator’s [FC DynHull Pine v6](../indicators/FC-Hull-Dynamic-Source.pine). `trade`, `demo` and `trade-all` use this policy only. The older Either/16 charts and v4 closed-label benchmark are historical tools, not signals for this bot.

**Timeframes pending.** `hull.eligible_timeframes: []` blocks preflight, observation and arming before MT5 is initialized. Neither symbol profiles, `data.timeframes` nor `--timeframe` grant permission. Once the operator supplies the list, a flag can narrow it, never expand it. Do not interrupt an existing manager to install this change.

| Confirmed structure | Fresh raw Hull BUY | Fresh raw Hull SELL |
|---|---|---|
| Bullish | Close owned shorts first; a new BUY is eligible if flat | Close owned longs; no new short |
| Bearish | Close owned shorts; no new long | Close owned longs first; a new SELL is eligible if flat |
| Neutral | Close owned shorts only | Close owned longs only |

No midpoint, discount/premium, swing-zone, EMA or later-BOS requirement at the signal **or** on the quote used for a ticket. INIT/BOS/MSS, a wick touch, or bias alone is never an entry or an exit signal. The execution desk still requires a valid original SL and all existing order protections for **new** entries.

## Calculation and clocks

- Default length 24. Build the full Low and High Hull A series separately. Select Low in bullish structure, High in bearish structure. While neutral, `neutral_mode: high` matches Pine; `wait` suppresses raw signals there. B always uses close. Strict BUY means B > A now and B < **the same source’s** A on the preceding completed chart candle; SELL is the strict inverse. Switching source alone cannot manufacture a crossover. Fixed Pine OHLC-derived sources are supported; `custom` can map to a named local price source, not an external TradingView study feed.
- `structure_timeframe: null` is the indicator’s **Chart** setting: independent state per symbol and signal timeframe. Optional higher-TF bias is published only from a native close available at the chart bar’s **open**. A nested lower timeframe processes only finished structure bars, up to the observed tick for live charts. The override does not create a trading lane.
- The default swing has three bars on either side. Structure tests the previous guard and swing levels by **strict closing price** first, then consumes the newly confirmed pivots. MSS has priority; continuation and failed counter-swings update protection exactly in that order. Pivots from the qualifying break window can arm the new leg. Wicks/equal closes do not reverse the trend. The protected swing is for direction, not a half-range entry test or the initial SL.
- The bot samples a verified native forming bid candle on fresh MT5 quotes; it does not wait for its close. A live cross can disappear later. An independently fresh confirmed close can also qualify. Warm-up/known-at-attachment signals are not trades; the same observed cross cannot re-enter on every tick or again at the close. Different feeds and polling mean Pine and MT5 need not see every identical tick.
- A raw opposite signal requests a market close of **all owned opposite tickets on that instrument** before deciding on a replacement, even if that signal cannot open (wrong trend, invalid initial stop, or new-entry risk veto). A market request is not a guaranteed fill. A stale quote defers the latched close until a usable quote; ambiguous replies require reconciliation, never a blind resend. Broker SL/TP stay in place. A replacement needs an independently trend-aligned cross, verified flatness and normal admission. Only entry-qualified signals vote on a replacement. If opposite directions both qualify at the same timestamp, skip the new entry; protective exits are still honored.

## Upgrade the local configuration

Keep the existing broker aliases, lots, risk limits, magic number, ladder and ticket state. In `hull:` use `entry_timing: live_hull_cross` and initially `eligible_timeframes: []`. Under `indicator:` use `source: dynamic`, `hull_len: 24`, `swing_len: 3`, `structure_timeframe: null`, `neutral_mode: high` and `exit_opposite: true`. Remove the retired `signal` and `ema_length` keys. Old `either`, old clock values, or disabling opposite exits are rejected rather than silently changing a running policy. A private YAML is not overwritten, and a running process is not updated by editing these files.

The default initial stop, spread-inclusive per-ticket TP, 70/30 ladder and broker safeguards are documented in the [operating guide](HULL_DEMO.md#which-candle-supplies-the-stop). No performance result is claimed for this replacement.
