# Archived chart v3 · superseded

**Historical behavior only; do not install v3 for the current rules.** [V4 scanner and charts](HULL_STRUCTURE.md) remove the watch, reversal limit, and retest entry entirely. The remainder describes the former chart-only release.

**A watch is not an entry. A CHoCH close is not a fill.** The TradingView and MT5 custom indicators use the same closed-candle rules on the chart timeframe. Neither sends orders. The Python demo scanner and its replay **still use `protected_swing_v2`**, not these chart rules.

## Trend entries: the correct half of a proven range

A continuation break of structure (**BOS**) is a strict close beyond the prior confirmed structural HH/LL after a pullback. That break establishes which swing is protected. Wait for the new outer HH/LL to be confirmed on this timeframe (default: 3 bars on either side; a newer equal wick wins). The midpoint is halfway between the protected swing and that confirmed extreme. There is **no extra minor pullback pivot** to wait for, and no backdating to the pivot candle.

| Established structure | Fresh Hull cross that can qualify at the close | Never a trend entry |
|---|---|---|
| Bullish: protected higher low (HL) → confirmed higher high (HH) | **BUY** if `HL < close ≤ midpoint` | SELL anywhere in that bullish range; BUY in the upper half or on the HL |
| Bearish: confirmed lower low (LL) → protected lower high (LH) | **SELL** if `midpoint ≤ close < LH` | BUY anywhere in that bearish range; SELL in the lower half or on the LH |

The midpoint is included; the protected swing itself is excluded. The first pivot break seeds context only. A CHoCH resets the trend bands: it does not itself establish the next BOS range. Internal pivots and wick sweeps do not move the guard. A subsequent BOS can promote the intervening deepest retracement wick only if it is a genuine higher low / lower high; otherwise the old protection survives.

A valid trend cross still freezes the band, stop and chart R target at its close. The **simulated continuation** tries the next adjacent open once; a price outside the frozen band or bracket vetoes the fill without erasing the original suggestion. This is separate from the reversal limit below.

## Reversals: watch → confirming close → later retest

| Stage | Bearish structure looking for a BUY | Bullish structure looking for a SELL |
|---|---|---|
| **1 · Watch only** | After bearish BOS **and** a confirmed new LL, a fresh BUY with `new LL ≤ close < old broken LL` may arm. | After bullish BOS **and** a confirmed new HH, a fresh SELL with `old broken HH < close ≤ new HH` may arm. |
| **2 · Confirm and stage** | A candle must **close strictly above the protected LH**. If the BUY watch survived, or a fresh BUY cross occurs on this close, stage a simulated **buy limit at the broken LH**, subject to the chart's risk/slot checks. | A candle must **close strictly below the protected HL**. A surviving SELL watch or fresh SELL on that close can stage a simulated **sell limit at the broken HL**. |
| **3 · Fill or no trade** | Starting on the **next** candle, a low at/below the buy limit can fill the chart plan. No later retest, no BUY fill. | Starting on the **next** candle, a high at/above the sell limit can fill it. No later retest, no SELL fill. |

A wick or a close exactly on the guard is **not** CHoCH. The confirmation candle's own high/low cannot fill its new limit. No second Hull cross is required at the touch. An intervening **opposing Hull cross cancels the watch**, even if that cross would fail entry filters. A same-close source conflict cancels rather than choosing a side. A watch belongs to its protected range; a superseding range or a missing time bar clears it. A new eligible cross may start a new watch.

**Example from the synthetic checks.** A BUY cross arms below bearish protection at **92.281**. A wick to **92.294** closes at **92.149**: still no order. A later candle closes at **92.691** with **no new cross**. Its low of **92.198** doesn't count as a retest: the limit did not exist yet. The following candle stays above 92.281; the candle after that touches **92.281**, producing the simulated BUY. The mirrored SELL case uses **107.719**. These are test candles, not live deals.

## How the chart simulation handles uncertainty

- A limit that passes the direction, optional EMA, stop, capital and chart-position checks reserves one chart slot. It is fixed at the **old broken guard**, not a midpoint, a new origin, or the confirmation close. Its bracket is frozen at confirmation. The default stop uses up to 7 previous local wicks, bounded by the last opposite cross, plus 0.1 × ATR(14); the confirmation candle is excluded from the local wick window. Reversal target R is measured from **limit to stop**. Turning on another stop or EMA option can veto a plan.
- There is **no fixed bar-count expiry**. An untouched limit is cancelled at the next opposing signal/conflict, new structural break or discontinuity in chart time (including a session gap). A continuous later bar's eligible touch is processed **before** its closing signal; that close cannot retroactively cancel an intrabar touch. Across a gap in timestamps the old order is discarded, not assumed filled in missing data. A fresh signal on the new close is evaluated normally.
- On adjacent candles a normal touch fills at the limit. A favorable opening gap can improve the simulated fill, never make it worse than the limit. An open through the frozen stop, an invalid bracket, unavailable size or an occupied slot vetoes the attempt; a vetoed touch is not retried. For an intrabar touch, the high/low does not prove a favorable target came *after* entry: require the close to reach the target to credit it on that candle. If both stop and target could have been hit, take the stop first.
- An existing position's **opposite-signal exit remains independent**. A selected opposite cross with a valid stop and optional EMA may queue its exit for the next open even if it cannot open a replacement. A stored watch without a new cross does not invent a position exit.
- This is candle-based, single-position accounting. No live quotes, spread, liquidity, queue position or broker fill is inferred. Change of history, feed, timeframe, settings or tick size can change the results.

## Use the indicators

**TradingView:** paste the entire `indicators/FC-Hull-Either-16-Structure.pine` into a new Pine Editor indicator, save and add to chart. Look for **FC Range 3**. Remove older copies and recreate alerts from the updated script; choose **Once Per Bar Close**. The two *retest limit ready · not filled* alerts are separate from the selected-view BUY/SELL alerts.

**MT5 desktop:** put `MT5/FC-Hull-Either-16-Structure.mq5` in `MQL5/Indicators`, compile that file with **F7** in MetaEditor, refresh Navigator and attach **FC Hull Range Retest v3**. No EA, `.ex5` download, Algo Trading, or scanner restart is required. See the installation guide for buffer indices and the optional local smoke check.

**What to look for:** gold dots = watch guard (no order); blue marker = limit staged (not filled); blue line = unfilled limit; the panel shows the current reversal stage. Leave **Valid suggestions only** selected for trend suggestions and actual simulated reversal fills. Use **Simulated fills only** to exclude unfilled continuation suggestions. **Structure only** includes band-crosses and later retest touches before chart risk/slot checks. **All Hull crosses (debug)** intentionally shows countertrend raw crosses and is not an entry view. The Data Window separates armed, confirmed, staged, touched, filled, and cancelled events. The chart fill price is a diagnostic; the vertical position of a BUY/SELL caption is not its price.
