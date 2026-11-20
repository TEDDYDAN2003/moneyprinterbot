# Install FC Hull Range Retest v3 on MT5

This is a **custom chart indicator, not an Expert Advisor**. Its limits, fills and P/L are simulated from chart candles. It cannot read your account, submit an order or change the Python demo scanner.

1. In MT5 Desktop, choose **File → Open Data Folder**. Copy `FC-Hull-Either-16-Structure.mq5` to `MQL5\Indicators` (not `Experts` or `Scripts`). If upgrading, remove the old chart attachment before adding the new one. The source has no custom include dependencies.
2. Open **that `.mq5` file** in MetaEditor and press **F7 / Compile**. Check the Errors pane; MetaEditor creates the `.ex5` locally. No precompiled binary is supplied. Do not paste the TradingView `.pine` file into MetaEditor.
3. Back in MT5: **Navigator → Indicators → Refresh**. Attach **FC-Hull-Either-16-Structure**. Verify that the short name is **FC Hull Range Retest v3**, not Protected v2. M1, M2 and M5 each compute their own native range. Neither Algo Trading nor a scanner restart is needed.
4. Under **Inputs**, leave the view on **Valid suggestions only**. Enable **Draw allowed BUY and SELL bands** if you want to see the active half-range. Group **8 | Reversal stages** controls the gold watch dots, blue staged-limit marker / waiting line, and the optional *limit ready (not a fill)* popup. All popups are off by default. Selected-view BUY/SELL popups are separate.

## Read the marks

- In a bullish BOS range, only a fresh BUY from **above the protected HL through the midpoint** qualifies. In a bearish BOS range, only a fresh SELL from **the midpoint to strictly below the protected LH** qualifies. No extra minor pivot is needed. A continuation suggestion still needs an adjacent in-band open to simulate a fill.
- An opposite cross in the former post-extreme entry zone can **arm a watch only**. An opposing signal cancels that watch. For a BUY, require a strict close above the protected LH; for a SELL, strictly below the protected HL. A wick or equality fails. A surviving watch or a matching fresh cross on that close can stage a chart limit at the **broken** guard.
- **Staged is not filled.** The new limit is first eligible on a later candle. No retest means no reversal entry. In the default view a reversal BUY/SELL label is drawn only on a simulated fill. `Structure only (bands / retests)` can show an unexecutable touch; `All Hull crosses (debug)` deliberately shows raw, unqualified crosses. The **Simulated fills only** view excludes close-only continuation suggestions.
- For cancellation, session gaps, price improvement, ambiguous OHLC exits, position occupancy and examples, see [the full chart rules](../docs/HULL_CHART_V3.md). The [scanner's v2 rules](../docs/HULL_STRUCTURE.md) are intentionally different. Its real deals and this chart's panel should **not** be expected to match.

The forming candle can update its Hull/EMA lines and display the prior committed range/watch/limit. Decision **events** stay zero until a candle closes. MT5 observes the close on the first tick of the next candle. A retest can trade through intrabar but the chart reports the simulated fill once the **touch candle closes**. No historical popups on attachment/backfill. The most recent 100 selected marks have text captions by default; native arrows remain on older history. The price of an arrow/caption is not the fill price.

## Data Window / CopyBuffer

Use **shift 1** for the most recent closed candle. Sides are `+1` BUY, `−1` SELL, `0` none. Price buffers use `EMPTY_VALUE` when absent. The old public indices 10–32 are retained; the meaning of *structure*, *valid* and *fill* now follows v3.

| Buffer | Meaning |
|---:|---|
| 10–14 | Hull B, its colour index, high/low Hull A, optional EMA |
| 15–18 | Visible BUY low/high, SELL low/high band edges |
| 19 / 20 / 21 | Selected-view BUY / SELL marker prices, raw conflict mark in debug view |
| **22 / 23 / 24 / 25 / 26** | Raw cross / structure pass (trend cross or first retest touch) / valid chart suggestion / any simulated fill / source conflict flag |
| 27 / 28 / 29 | Protected HL/LH, confirmed structural extreme, candidate pullback wick (not protected) |
| 30 / 31 / 32 | Bias; break **on this close** (`±2` CHoCH, `±3` BOS); pullback seen (`1/0`) |
| 33 / 34 | Visible watch guard / resting executable limit price; empty if stages are hidden |
| 35 / 36 | BOS range midpoint / current watch side (state, not events) |
| **37 / 38 / 39** | Watch newly armed / eligible CHoCH confirmed / executable limit staged **on this close** |
| **40 / 41 / 42** | First later structural retest touch / actual simulated retest fill / cancelled watch or unfilled retest (signed side) |
| 43 / 44 | Visible staging marker price (not a fill) / simulated fill price (any fill, independent of mark visibility) |

For example, `22 = +1`, `37 = +1`, `24 = 0` means **watch BUY**, not enter. Later, `38 = +1`, `39 = +1`, `41 = 0` means **BUY limit staged**, not filled. On a subsequent `41 = +1`, `44` gives the simulated buy price. A confirmation can have `22 = 0`. A risk veto can leave `38 ≠ 0` and `39 = 0`; that path never produces an executable retest from that confirmation.

## Verification and platform limits

Offline checks executed the delivered Pine source in **PineTS** and the delivered MQL5 calculations and `OnCalculate` scheduler through a **C++ host shim**. The v3 comparison covers 29 synthetic scenarios, 28,936 closed-bar decisions and 62 fields per bar, plus 62 hand-specified BUY/SELL paths. It includes stored-watch confirmations without another cross, real mirrored retests, forming-candle suppression, incremental vs full history, and separate limit/entry alerts. See [QA instructions](qa/README.md).

This is not a TradingView or MetaEditor native compilation and not a P/L backtest. MT5 commonly charts broker bid OHLC. Match feeds, settings, tick size and loaded history when comparing platforms; session-shortened and custom bars can differ. MT5 uses the expected time-bar end (calendar month for MN1) to detect gaps.

**Optional local check:** after compiling, copy `optional/Check-FC-Hull-Structure.mq5` to `MQL5\Scripts`, compile it, then run it once on a liquid chart with enough history. It loads the v3 indicator with defaults, reads closed-bar buffers, checks that forming-bar events are zero, and prints a summary in the Experts tab. It cannot trade.
