# FC Hull Either 16 · Market Structure Suggestions for MT5

**Install `FC-Hull-Either-16-Structure.mq5` as a custom indicator, not an Expert Advisor.** It reads native chart candles; does not inspect your account or broker tickets; and cannot submit, modify or close an order. The Windows journal in the other folder reads actual closed deals independently.

## Put it on your chart

1. In your **MT5 desktop terminal** choose **File → Open Data Folder**. Open `MQL5\Indicators` and copy `FC-Hull-Either-16-Structure.mq5` there. The single source file has no custom include dependencies. Do not put it in `Experts`.
2. Open that file in **MetaEditor** (F4 from MT5), then press **F7 / Compile**. MetaEditor creates the `.ex5` next to the source. Check the **Errors** pane in your own build; no precompiled binary is supplied.
3. Return to MT5 → **Navigator → Indicators → Refresh**. Drag **FC-Hull-Either-16-Structure** onto the desired symbol/timeframe. The original Hull workflow uses **M1, M2 and M5**; each chart maintains its own structure. Algo Trading, DLL imports and a logged-in trading account are not required for the indicator to calculate from loaded bars.
4. Under **Inputs → 1 | Suggestions and alerts**, leave **Valid suggestions only** selected. Turn on **Draw allowed BUY and SELL bands** to inspect the price areas. To hear/see fresh selected chart signals, enable the BUY and/or SELL popup inputs. They are off by default; hiding marks does not disable an enabled alert. No historical popup storm on attachment.

## What is preserved

- **Independent low/high Hull A curves**; one close-based Hull B; default length 16, Either source, strict crosses (equality is not a cross). Aligned events count once; opposed raw candidates cancel. Directional, Low only, High only and optional alternation are also available.
- **Structure on confirmed bars only.** The initial strict close break seeds context. CHoCH alone is not an entry. Newest equal wick wins the 3-left/3-right pivot test. Bull HH → later HL allows BUY in `[HL, midpoint]`; bear LL → later LH allows SELL in `[midpoint, LH]`. After continuation BOS + a confirmed new extreme, countertrend SELL needs `(old broken high, new HH]`; countertrend BUY needs `[new LL, old broken low)`. No extra pullback required for these two bands. A later pivot never backdates an old cross.
- **First eligible while a chart slot is available.** Same-side repeats while still open are rejected. A new cross after flat can qualify in the same region. Raw opposites with executable stop/EMA may queue an exit independent of structure. A replacement must qualify. The close-at-candle-extreme ambiguity guard and stop-first assumption on double-touch candles follow the supplied Pine.
- **Stop and open check unchanged.** Default stop uses up to 7 prior local wicks, bounded by the preceding selected opposite, with 0.1 × Wilder ATR14. Signal candle excluded. Outward rounding uses `SYMBOL_TRADE_TICK_SIZE` (point fallback). The close freezes its band, stop and R target. At the next adjacent chart open the simulated price, frozen band and bracket are rechecked; missing bars or out-of-band opens veto, never erase the close suggestion.
- All four label/alert views: valid close, structure-only, simulated fill, raw debug. A chart use-case, not the bot's multi-ticket 70/30 ladder. Optional EMA, permitted sides, pullback/pivot stop, costs, slippage, position sizing, target R and executable opposite exits retain the supplied defaults. The panel is **chart-simulated** P/L, not MT5 account P/L.

## MT5 display and timing

- The live bar can update its Hull/EMA lines. All decision buffers stay zero until it closes. MT5 confirms a bar on the **first tick of the next bar**; a simulated fill at the prior open is marked only once the **fill candle** is confirmed, just like the selected Pine view. Popups only fire for a just-observed adjacent close, not history catch-up across a gap. An unformed new tick can delay notification.
- Permanent native arrow buffers show marks throughout loaded history. For legibility, only the most recent 100 selected marks have text captions by default (set to 0 for arrows only). Fills use native chart shading with background-blended colours. Plans are bounded by the requested retention; future endpoints are provisional until the requested number of actual bars exists. Change the stats panel corner or turn it off.
- Match **OHLC feed, timeframe, warm-up history, tick size and settings** when comparing to TradingView. MT5 commonly charts broker bid candles. Pine's `time_close` is approximated by the expected end of the MT5 time bar (calendar month for MN1); irregular, session-shortened and custom bars can differ. Time labels are broker chart time. History corrections/reloads recompute from available bars.

### Data Window / CopyBuffer reference

| Buffer | Meaning |
|---:|---|
| 10 | Hull B (close); 11 is its colour index |
| 12 / 13 / 14 | High Hull A / low Hull A / optional EMA |
| 15–18 | Visible BUY low/high and SELL low/high band edges |
| 19 / 20 | BUY / SELL marker prices for the selected view (`EMPTY_VALUE` when absent) |
| 21 | Conflict marker in debug view |
| **22 / 23 / 24 / 25 / 26** | **Raw / structure-qualified / valid suggestion / sim fill / conflict flag** |

Use **shift 1** for the last closed candle. Directional decision buffers use `+1` BUY, `−1` SELL, `0` none. Buffer 26 is `1` for a raw conflict, else `0`. Hiding chart marks never removes the diagnostic decision codes. For example, 22 = +1 with 23 = 0 is a raw BUY that failed the market-structure test. Buffer 24 = +1 is a valid **chart suggestion**, not proof of a broker fill.

### Verification

The actual calculation source and `OnCalculate` scheduler were run offline through a C++ compatibility shim. On 23 synthetic PineTS scenarios (27,758 closed-bar comparisons; 40 fields per bar), signal modes, pivots/bands, valid suggestions, frozen open checks, position and accounting states matched the supplied Pine within the test tolerance. Additional lifecycle checks covered repeated updates to the forming bar, alert gating and full vs incremental replay. **This is not a native MetaEditor compile or a performance backtest.**

Optional: after compiling the indicator, copy `optional/Check-FC-Hull-Structure.mq5` to `MQL5\Scripts`, compile it, and run it once on a chart. Its Experts-tab output checks that the compiled indicator loads, forming-bar decisions are zero and the diagnostic buffers agree. Use an ordinary liquid chart with enough closed history. The script does not place orders.
