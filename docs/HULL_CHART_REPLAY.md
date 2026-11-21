# Replay Dynamic Hull 16: Free Chart and TradingView

These are **indicators for manual chart replay**, not order senders. The win rate belongs to simulated entries in this indicator, not trades you click in the platform simulator, Strategy Tester orders, MT5 positions or the Hull Journal. Use ordinary time-based OHLC candles.

## The Free Chart: paste these two files

| Build Indicators → New Indicator | File / setting |
|---|---|
| **Inputs** | Paste the **entire JSON array** from [`FC-Dynamic-Hull-Replay.inputs.json`](../indicators/freechart/FC-Dynamic-Hull-Replay.inputs.json). It defines the settings, not the indicator logic. |
| **Script** | Paste the **entire JavaScript** from [`FC-Dynamic-Hull-Replay.js`](../indicators/freechart/FC-Dynamic-Hull-Replay.js), including its final `return`. Do not add a function wrapper, `export`, or Markdown fences. |
| **Settings** | Name: **FC Dynamic Hull 16 Replay**. **Overlay: on**. Choose precision to match the symbol, then **Apply**. Precision changes formatting, not contract sizing. |

1. Open the instrument you will actually replay. Start with a **15-minute** candle chart and load earlier history to warm the Hull and structure. The overlay supports standard minute charts from 1m through 45m. Its **15m, 30m and 45m** test books always use their own signal candles, regardless of the focus setting. The script uses `bv.security` for other signal timeframes; it does not make up M45 by substituting M15 signals.
2. In indicator settings set **Chart minimum price tick** (the smallest tradable/observable chart increment). For **Gross USD** also set **USD per broker tick at 1.00 lot**, the **price size** of that broker tick if it differs from the chart tick, your **lot size** and your **gross TP in USD**. Set **Point size for display** if needed. The documented chart metadata does not supply a verified broker cash tick value; zero means **not configured**, not a zero-distance win. Recheck these values whenever you change symbols. If you only want to test a known price distance, choose **Chart ticks (no USD conversion)** and enter the number of chart ticks instead.
3. To score **only the replay period**, set **Test from UTC** to the replay start (e.g. `2026-11-18T08:00Z`), keeping earlier bars loaded for warm-up. **Test through UTC** is optional. A date without a time means midnight **at the start of that UTC date**. Blank starts with loaded chart history. With no explicit start, scrolling in older history can change the sample; rewinding a fixed sample intentionally removes later outcomes.
4. Start **Bar Replay** and advance. Leave **Next visible bar open** selected if the newest candle may still be forming: the newest visible **opening timestamp** is the proof that earlier candles have closed. A 15m candle ending at 10:15 can register when the 10:15 candle appears. If each replay step reveals **one fully finished candle**, choose **Completed-bar steps only** to include that candle’s close without an extra step. **Do not use this option in live or intrabar replay.** No wall clock is used to finalize a historical bar.
5. Watch **Open TP price** and the labelled dashed guides: **teal 15m, blue 30m, lilac 45m**. A confirmed entry freezes the absolute target even if price changes. A settled exit removes only that trade’s guide. A same-close reversal replaces it. `BUY?` / `SELL?` means a prior BE is possible; the study has not proved the position is flat. After the test end the book stays frozen for reporting and no current target is drawn. Focus controls only the optional Hull curves and counted-entry/exit audit marks. The board and target guides follow the **rightmost visible candle**, never a later cached result. Panning back shows the earlier snapshot; return to the replay edge to continue. The chart’s own price-label switch controls figure tags, not these TP guides.

**Sizing check, not a contract assumption:** suppose the chart tick is `0.10`, the broker pays **$1 per 1.00 price move at 1 lot**, and you test **1 lot / $5 gross**. Enter `0.10` for chart tick, `1` for USD/tick, `1` for broker tick price size. From `25,000`, a BUY targets `25,005.0`, and a SELL `24,995.0`. With a display point of `0.10` this is **50 points**. If you get a different result, check the broker tick **size** as well as its USD **value**; do not infer them from an index name. Non-USD contracts need the appropriate USD tick value; Free Chart keeps your manual conversion constant through the study.

## TradingView: show targets for already-open studies

Paste [`FC-Hull-Dynamic-Source.pine`](../indicators/FC-Hull-Dynamic-Source.pine) in **Pine Editor**, **Save** and **Add to chart** (or update the existing instance). In **Display** leave **Show open TP lines (15m / 30m / 45m)** on. This is separate from the optional *Mark counted test entries and exits* dots. The **Open TP price** table row remains available if lines are hidden or two lanes have identical targets. The labels use the native **entry-close time**, not an earlier live suggestion. A flat lane never displays its previous trade’s TP as open. The change is display-only: it does **not** change the Pine entry list, scoring, alerts or USD sizing.

For a platform comparison, use **Length 16**, **Dynamic**, **High** while neutral, **3** swing bars, and **blank structure timeframe** (each lane’s own structure). Set the same candle feed/session if available, manual gross sizing, calendar window, session windows, inspection interval and BE percentage on both. The Free Chart port covers the native-structure replay study; it does not import TradingView external source plots, optional structure-timeframe overrides, all the Pine swing drawings, or live alert subscriptions.

## Read the table

- **One independently held slot per timeframe.** Enter on a confirmed, trend-aligned strict Hull cross. The first aligned cross after an MSS **is allowed**. An MSS alone is not an entry **or** a chart-study exit. This is deliberately different from the Python bot’s own-lane MSS flatten, first-cross skip and executable-quote checks.
- Only bars **after** the entry can settle it. The study inspects 1m candles (optionally 5m) in chronological order when coverage and parent OHLC match. Otherwise it uses the whole signal candle without assuming whether high or low came first. A positive BE threshold arms **after a qualifying signal close**, not on a smaller candle’s wick. Zero means immediate BE, not disabled. **TP first = win; proven BE or an opposite close first = loss**, even if the opposite close is profitable. A trade that must be flat but could have hit either TP or BE first is `?`.
- **Overall / Asian / London / New York / NY–London KZ** use the **entry close** to assign sessions. Sessions may overlap; Overall counts an exit once per lane. London and New York each follow their own local daylight saving time. **All TFs sums counts**, not percentages. Open/possibly open slots do not enter `n`. When `U` outcomes have unknown order, the interval is `wins/n` through `(wins+U)/n`; it is not a confidence interval. `— (0)` means no settled trades; `n/a` means the study could not calculate a valid rate.
- **Different feeds are not the same test.** History depth, market hours, shortened candles, missing inspection bars, feed precision and the available native 45m series can all change results. In Free Chart, intraday closes use the nominal duration capped by an earlier next open or a reported exchange-session end. There is no guaranteed holiday/early-close stamp for every symbol. An incomplete final inspection slice falls back to parent OHLC. `1m coverage` is the share of evaluated **held** bars with valid fine data. A loading/unavailable lane is not a zero-trade lane; the pooled rate waits for all three. If a lane stays unavailable, check symbol history/interval support (try a 45m host for the 45m series), the date window and sizing. A reported native history gap is not silently skipped.
- Clearing/reapplying the indicator recomputes from available history. It does not persist fills, send orders, include fees/spread, or mirror the bot’s initial SL or TP ladder. Use broker deals and Hull Journal for actual results.

**Platform reference reviewed:** [The Free Chart Custom Indicators guide](https://thefreechart.com/docs/custom-indicators) and [developer API reference](https://thefreechart.com/custom-indicator-reference.md) (editor tabs, candle timestamps, `bv.security`, `bv.securityLowerTf`, `calc`, figures, drawing and timezone helpers).

### Maintainer checks (fabricated candles only)

From the repo root, with Node 20+:

```sh
node tests/freechart/replay_contract.cjs
# Install pinets@0.13.1 in a temporary directory, then use its absolute module path:
PINETS_MODULE=/path/to/node_modules/pinets node tests/freechart/pine_parity.cjs
PINETS_MODULE=/path/to/node_modules/pinets node tests/pine/hull_stats_contract.cjs
```

The first test runs the actual editor factory with a documented-API shim and checks replay/rewind, open-target lifecycle and unavailable inputs. The second compares the JS to the actual study run in PineTS, native bar by native bar. PineTS is not TradingView's compiler, and the shim is not The Free Chart's hosted engine. Neither is evidence of investment performance.
