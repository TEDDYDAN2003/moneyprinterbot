# Dynamic Hull: signal, entry and exit

The scanner’s signal reference is the operator’s [FC DynHull Pine v6](../indicators/FC-Hull-Dynamic-Source.pine). `trade`, `demo` and `trade-all` use this policy only. The older Either/16 charts and v4 closed-label benchmark are historical tools, not signals for this bot.

**Timeframes pending.** `hull.eligible_timeframes: []` blocks preflight, observation and arming before MT5 is initialized. Neither symbol profiles, `data.timeframes` nor `--timeframe` grant permission. Once the operator supplies the list, a flag can narrow it, never expand it. Do not interrupt an existing manager to install this change.

| Confirmed structure | Fresh raw Hull BUY | Fresh raw Hull SELL |
|---|---|---|
| Bullish | Close owned shorts first; a new BUY is eligible if flat | Close owned longs; no new short |
| Bearish | Close owned shorts; no new long | Close owned longs first; a new SELL is eligible if flat |
| Neutral | Close owned shorts only | Close owned longs only |

**After each confirmed MSS/CHoCH, skip exactly the first trend-aligned Hull cross on each signal lane.** A matching cross on the shift close counts as first. INIT/BOS do not arm or reset this rule; opposite raw signals still exit and do not spend it. The skipped bar stays blocked on every tick and at its close. A later distinct aligned cross is eligible, subject to normal order checks. A raw bot signal with an unusable initial stop is not an otherwise-valid candidate and does not use the skip. The first candidate is counted as a signal, not a fill; a desk/risk veto never refunds it.

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

## Pine win-rate table (not a bot configuration)

The 15m, 30m and 45m rows each evaluate the same Hull and configured structure on their own candles. With **Chart**, each row uses its own native structure. The optional override follows the same confirmed-structure clock as the chart. One open position per row; open positions at the end of the test window are not in the win-rate denominator. Chart alerts may be live; historical table entries and opposite-signal exits use the signal candle’s close.

Set lot size and USD target. TP distance in ticks is `ceil(target USD / (lots × USD per tick per lot))`; distance in broker points is `ticks × minimum tick / broker point size`. Use the tick-value override for a different broker contract. The table uses gross price movement, not broker spreads, fees, the real SL or the profit ladder. Immediate break-even is the default; a positive percentage arms protection from the bar **after** a favorable close reaches that fraction of the TP distance. An active break-even or any opposite raw cross before TP scores a loss. Price levels are checked before the close signal; ambiguous TP/active-BE order scores a loss unless the open itself already reached TP. No market replay or performance estimates are included. The three comparison intervals do not authorize any live bot timeframe.
