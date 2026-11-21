> This MT5 chart study is historical v4. Use the [Dynamic Hull reference](../indicators/FC-Hull-Dynamic-Source.pine) for the current Python bot.

# Install FC Hull BOS Half-Range v4

This is a **custom indicator**, not an Expert Advisor or a connection to the Python scanner. It displays the same closed-bar entry rules, but its fills and P/L are chart simulations.

1. In MT5 Desktop choose **File → Open Data Folder**. Copy `FC-Hull-Either-16-Structure.mq5` into `MQL5\Indicators` (not `Experts`).
2. Open **that file** in MetaEditor (F4) and press **F7 / Compile**. Check the Errors pane; no precompiled `.ex5` is supplied. Return to MT5, refresh **Navigator → Indicators**, remove the old chart attachment and add **FC Hull BOS Half-Range v4**. No DLL, account permission or Algo Trading is needed to calculate on loaded candles.
3. Use **Valid suggestions only** to see fresh trend crosses that pass the structure, chart-stop and slot checks. Enable the BUY/SELL bands to see the eligible half-range. Use **Simulated fills only** for next-open chart executions, or **Structure-qualified crosses** to inspect the band gate alone. **All Hull crosses (debug)** is for reference: a green **×** (up) or red **×** (down) sits on Hull B on the candle that confirms the cross. There are no text captions in that view, even with captions enabled; an amber **×** above a candle means opposing sources, not a directional signal. Diagnostic popups remain separate from entries.

**What was removed:** the old watch dots, staged-limit marker/line, limit-ready alerts and retest fills. A strict close through the old protected swing updates structure only. Neither a matching cross on that close nor a later touch of the broken level qualifies. A new BOS with its confirmed swing range and a fresh completed **trend-side** cross are required. [Exact BUY/SELL inequalities, an example, and scanner differences](../docs/archive/HULL_STRUCTURE_v4.md).

### Data Window / CopyBuffer

Use **shift 1** for the last closed candle. Signed decisions are `+1` BUY, `−1` SELL, `0` none. Event buffers stay zero while the current candle is forming. MT5 reports a close on the first tick of the next candle; plotting a label is not an executable price. Compare matching data, timeframe, tick size, inputs and warm-up. MT5's expected bar-end check can differ from TradingView on shortened sessions and custom bars.

| Buffer | Meaning |
|---:|---|
| 10–14 | Hull B; colour; high/low Hull A; optional EMA |
| 15–18 | Visible BUY low/high and SELL low/high band edges (`EMPTY_VALUE` if hidden/absent) |
| 19 / 20 / 21 | Selected up / down marker, debug conflict marker. In All Hull crosses, 19/20 use the Hull B value on the confirmed candle rather than the candle low/high. |
| 22 / 23 / 24 / 25 / 26 | Raw cross / structural trend cross / valid suggestion / simulated next-open fill / source conflict flag |
| 27 / 28 / 29 | Protected HL/LH, confirmed HH/LL, candidate pullback wick (not protected) |
| 30 / 31 / 32 | Bias, structural break on this close (`±2` CHoCH, `±3` BOS), pullback seen (`1/0`) |
| **35 / 44** | Confirmed BOS midpoint / actual simulated fill price; price absent = `EMPTY_VALUE` |

Prior v3 reversal indices are **retired, not repurposed**: 33, 34 and 43 are always `EMPTY_VALUE`; 36–42 are always zero. No hidden order or retest state exists. A value at index 31 is not an entry: e.g. `31=+2, 22=+1, 23=24=0` is a bullish CHoCH with a raw BUY and no valid entry.

Optional: put `optional/Check-FC-Hull-Structure.mq5` in `MQL5\Scripts`, compile and run it once with enough closed history. It only reads indicator buffers; inspect the Experts tab. Offline tests use PineTS and a C++ compatibility shim (including native scheduler source), **not** the TradingView compiler, MetaEditor or broker execution. [QA instructions](qa/README.md).
