# Hull v4: closed trend crosses only

**Applies to the Python demo scanner, the ordered-quote replay, and the two structured chart indicators.** The shared entry policy is `bos_half_range_close_v4`. A change of character (**CHoCH**) updates the bias. **It is never an entry, a watch, a limit order or permission to enter on a later retest.**

## What can open a new position?

| Native timeframe state | Qualifying fresh Hull cross on the completed candle |
|---|---|
| Bullish continuation BOS, protected higher low (HL) and confirmed higher high (HH) | **BUY** only when `HL < close ≤ (HL + HH) / 2` |
| Bearish continuation BOS, confirmed lower low (LL) and protected lower high (LH) | **SELL** only when `(LL + LH) / 2 ≤ close < LH` |
| Initial context, CHoCH, or a BOS without its new confirmed extreme | **Neither side** |

The midpoint counts; the guard itself does not. Only the aligned side has a band. There is **no direct countertrend entry** at either end, no storing of an opposite cross, and no extra minor pullback pivot required after the dealing range is known. A later price entering an eligible band without a **new** cross does nothing. M1, M2 and M5 maintain independent structures.

1. Confirm local swings using the configured left/right window (3/3 by default). A newer equal wick wins. The first close through a known pivot seeds context only. A later strict close beyond a confirmed structural high/low after a pullback proves a continuation **BOS**. Only now can the intervening deepest higher low / lower high become protected. A minor pivot cannot replace the guard by itself. The new outer HH/LL must be confirmed beyond the break; ranges are not backdated.
2. A strict close **above** the old protected bearish LH, or **below** the old bullish HL, is CHoCH. A wick or a close equal to the guard is not. Even a matching Hull cross on the CHoCH candle fails the entry gate. The old trend range is gone; a new confirmed swing following CHoCH **by itself** is not enough. Require a later continuation BOS, its confirmed range, and an eligible fresh closed cross.
3. Test the fresh signal's **close** against its range. Freeze those edges. In the scanner, test the **actual BUY ask / SELL bid** against the same edges for every proposed leg. If the first usable quote is outside, skip: no waiting for a retrace. A short, non-renewable wait for an unavailable, stale or pre-close quote on an already qualified trend signal is a data-timing safeguard, **not a price order**.

**Example.** With a confirmed bearish LL of 100 and LH of 110, a SELL at 105 qualifies structurally; at 104.99 or 110 it does not. A BUY at 101 cannot arm anything. A later close at 110.40 flips bias only. A return to 110, with or without a fresh cross, is still not an entry on that basis. If a *later* bullish BOS proves an HL of 108 and a confirmed HH of 116, a **new** BUY close at 111.50 can qualify. A broker ask above 112 at execution vetoes it.

## Timing, exits and audit

- All signals use **completed chart candles only**. An M1 candle stamped 10:03 can qualify at 10:04, not at 10:03:15. Native indicator arrows are not broker fills: feeds, history, tick size, chart slot/stop settings and execution can differ. Chart fills simulate the next adjacent open; the bot checks actual quotes, spread, available margin, stop distances and ownership. No stale warm-up signals or restart catch-up entries.
- The existing **opposite closed-label exit** remains separate. If enabled, a fresh opposite cross with a legal stop and optional EMA can close an existing position without opening a replacement. CHoCH or a retest alone does not close a ticket. Lots, per-ticket base-plus-spread target, ladder, session, exposure and other safeguards are not relaxed.
- For simultaneous valid lanes: same direction → highest timeframe, opposing valid directions → skip. No fallback to an older vetoed signal, no duplicate batch while occupied and no topping up a partial batch.
- In scanner logs, `structure_break` reports the confirmed BOS or CHoCH; `closed_label` reports the raw cross. Inspect `structure`, `range_midpoint`, `zone_low/high`, their open-edge flags, `entry_setup`, `entry_side` and `entry_reason`. `structure_choch_context_only`, `structure_wait_continuation_bos`, `structure_wait_confirmed_bos_range`, `countertrend_not_an_entry`, and `outside_…` are vetoes. Only an actual broker deal establishes a broker fill. Historical journal records keep their original reasons; v3 chart fills were never broker deals.

## Upgrade and verify

- **Bot:** check `entry_policy: bos_half_range_close_v4`, `entry_timing: confirmed_close_only`, `entry_quote_policy: first_fresh_post_close_tick_v1`, and `choch_entry: none` in the preflight/start summary. Missing timing uses close-only. An existing `live_continuation_v1` value is recognized but mapped to **close-only**, reported by `retired_clock_migrated: true`. Unknown values are rejected. No private YAML, magic number, position, ladder state or ticket is migrated. Never replace an active armed process or clear its state to install this update.
- **TradingView:** paste the complete `indicators/FC-Hull-Either-16-Structure.pine` into Pine Editor, save and add **FC Range 4**. Remove v3 from the chart; delete its old *limit ready* alerts and recreate any desired v4 alerts **Once Per Bar Close**. Use *Valid suggestions only* for entries. Debug markers say RAW and are not entries.
- **MT5:** compile `MT5/FC-Hull-Either-16-Structure.mq5` in `MQL5/Indicators` with F7, refresh Navigator and attach **FC Hull BOS Half-Range v4**. [Inputs, buffers and local smoke test](../MT5/INSTALL-MT5.md).

[Offline cross-platform verification](../MT5/qa/README.md) · [worked test cases](../MT5/qa/v4-cases.csv). In the full bot checkout, see `docs/HULL_DEMO.md` for execution and funding. Historical v2/v3 studies and backtests measured different policies; they are not performance evidence for v4.
