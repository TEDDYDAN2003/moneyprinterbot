# Direct pullback limits · paired Dynamic Hull desk

**Opt-in, USD hedging demo only.** Keep the normal [paired signal](HULL_PAIRS.md): M5 → M3, M10 → M5 or M15 → M10, confirmed cross and both biases aligned. This route changes **how the winning batch enters**, not which chart wins. It sends BUY LIMITs **below** the crossed candle's close or SELL LIMITs **above** it. The pullback to the price is the one required move. No breakout first, stop-limit, market fallback, later top-up or second batch.

## Where the limit goes

Use the **closed native trigger candle** for all four OHLC prices and ATR, not the parent, next open, most recent swing or forming candle. Measure *from the close* along just one segment:

| Hull signal | Bullish candle (`close > open`) | Bearish candle (`close < open`) | Doji |
|---|---|---|---|
| BUY | Back down into its body | Down its lower wick | Down its lower wick |
| SELL | Up its upper wick | Back up into its body | Up its upper wick |

Desired travel = `retest_fraction × segment length`, initially **20%**. Bound it below by **one broker trade tick** and above by the lesser of the segment length and `retest_atr_cap × trigger ATR` (initially **0.10 ATR**). Choose the nearest legal price tick *inside those bounds*. SELL adds travel to the close; BUY subtracts it. No tick fits? **Skip**; don't widen the zone, move the SL or trade the market instead. The two fractions are configurable starting values, not fitted to a screenshot or validated as profitable.

**Example, not broker data:** A bearish SELL cross opens at `2001.20`, closes at `2000.00`, high `2002.00`. At a `0.01` trade tick and `1.20` ATR, 20% of the body is `0.24`, capped at `0.12`. **SELL LIMIT = 2000.12**; with the template stop cushion and no extra pips, the **raw Bid-chart SL is 2002.12**. At a placement spread of 0.06 the **broker SL is 2002.18**. For a SELL on a bullish candle closing at `2000.00` with a high of `2000.40`, use the `0.40` upper wick instead: limit `2000.08`. For the mirrored bullish BUY body, the limit is `1999.88`. These are executable order prices, not chart pixels: **Ask** must reach a BUY limit; **Bid** must reach a SELL limit. The bars used by this desk are Bid bars.

The stop stays beyond the original trigger wick; for a new SELL, add the current Ask-Bid spread to the raw Bid-chart stop and round up to the trade tick because the broker triggers that SL on Ask. BUY stops remain at their Bid-chart level. Include the applied buffer in modeled cash risk. Every sibling shares the frozen placement bracket. At its final send quote a SELL must still have enough buffer: if spread is now 0.07 but the attached buffer is 0.06, that leg is not sent, even inside the drift bound. This veto is not permission for a market fallback. No resting stop is widened to chase the spread. The pending entry must fit the broker's entry and bracket distances. Size and cash target rules are per ticket. Each sibling freezes the **same placement-quote** spread cushion in USD plus the configured base target. MT5 cannot tell us the future fill spread at placement. Both SL and an initial TP go to the broker with **every** request. After an exact fill, price the cash TP from the **actual** fill, without changing the original SL or adding the spread again. Keep the existing ladder or fixed-TP setting. A better entry can reduce distance to the fixed stop; it does **not** reduce the price move required from entry for the same gross cash goal, guarantee a fill, or guarantee net profit.

## The two clocks

1. **Dispatch** starts on an eligible, fresh quote after the close. No waiting for a break. If the limit is still untouched but inside the required placement gap, the original, unsent signal can wait for a legal quote **only within its original short window**. An observed pre-placement touch is a miss. The public entry window is **8 s from the closed trigger**, not 8 s per leg; quote age is at most 4 s. Native chart/quote verification stays on. One plan sets the price, lots, SL and TP; the single writer checks and sends siblings consecutively. It does **not** wait for a position, a history update or the preceding order to appear in the active-order list. Accepted-but-not-yet-visible orders count towards exposure. There is no atomic MT5 multi-order request. A slow broker call cannot be preempted: the **submit** has to be on time; the response can be later.
2. **Fill lifetime** is separate: acknowledged orders can rest until the **end of the very next trigger candle**, or the first disallowed session/maintenance second, whichever comes first. For M5 → M3 the maximum lifetime is three minutes from the signal close, not from the last placement. MT5 must advertise LIMIT + attached SL/TP + specified expiry; the actual `order_check` still decides. No GTC downgrade. If dispatch time runs out or the pullback starts early, stop adding legs; legal working siblings can still fill. A safety veto requests removal of verified orders. Never chase, replay on restart, or enlarge remaining tickets.

Slow requests **10** when funded, medium **4 or 3**, fast **7 or 6**. Existing caps, dedicated account checks, mandatory instrument sessions, spread, broker limits and funding still apply. While orders rest the desk watches for changed protection, unusable quotes/specs, foreign exposure, loss of pair context, opposite signals on its own parent/trigger, parent MSS and the kill switch. Cancellation is a request that can lose a race with activation. Confirmed partial or invalidated race fills are reserved for protective exit, not topped up. Brackets/server expiration work without the scanner; software exits do not. A disconnected terminal, a freeze or a rejected removal can leave an order working until expiry.

## Enable and observe on your Windows MT5 PC

1. Upgrade **while flat** (no positions, pending orders or ambiguous acknowledgements). Back up the private config, account state, audit logs and journal database. Use the same working directory, magic, account and journal `--out`. Do not extract over a running checkout or overwrite `config.demo.local.yaml`.
2. In the **existing `hull:` section** of your own full config, set the keys below. Keep your intentional parent selection, profiles, exposure limits and emergency settings. Do not add a second `hull:` mapping.

   ```yaml
   execution_mode: paired_structure
   entry_route: direct_retest_limit
   retest_fraction: 0.20
   retest_atr_cap: 0.10
   ```

   Missing `entry_route` still means `market_next_quote`. The retest route is invalid with M1/legacy execution. The checked-in template is **paper**, with market entry as its default.
3. In the bot root, inspect the explicit profiles in the launcher scripts, then use the same profiles for **read-only** preflight and observation:

   ```powershell
   .\run_preflight.ps1
   # Only if preflight succeeded and the account, pairs, limits and symbols are right:
   .\run_arm_demo.ps1 -Observe
   ```

   Expect `READ ONLY`, `entry_route: direct_retest_limit`, `new_ticket_target_policy: base_plus_placement_spread_v1`, native pair readiness, and `retest_capability: supported` on every selected broker symbol. Do not remove `-Observe` to test this. Shadow checks do not call `order_send` or assert what the broker would fill. A shadow touch is at most a **sampled executable-price touch** (polls can miss ticks).
4. For unresolved armed-demo orders use the original manager/profile/settings. A normal shutdown requests cancellation; restart cancels verifiable known tickets and protects race fills. It does not fill missing slots. An ambiguous acknowledgement, changed ticket identity or missing proof blocks entries for review; no matching by comment or guessing from a missing active order. Check MT5's Trade **and** History tabs, the log and state backup. Do not delete `state.json` to bypass the hold. Never switch manager or stop it with tickets needing supervision.

## Tell latency from an unfilled retest

Use the account-specific `audit_log` path printed at startup. You can summarize an existing log **offline**, no MT5 connection or credentials:

```powershell
$log = Read-Host 'Paste the full path of one shadow.jsonl or demo.jsonl'
& .\.venv\Scripts\python.exe .\tools\report_hull_retest.py "$log" --details
```

`pair_arbitration` records selected signals; `label_decision` includes vetoes. `retest_batch_plan` records planned count, OHLC/segment, price, stop, TP and expiry. `retest_placement_result` and `retest_dispatch` distinguish close→submit, send round trip, dispatch duration and planned vs acknowledged count. `retest_shadow_result` is **not** a broker receipt. `retest_fill_receipt` requires linked order, opening deal and position identifier plus volume, price and time. `retest_batch_settled` records terminal order outcomes, not necessarily closed positions. Missing log events are unknowns, not zero-profit trades.

In [Hull Journal](../HullJournal/START-HERE.md), `r15x10_<close>_<leg>` (or `r5x3`, `r10x5`) is distinct from a `p…` market entry. Entry details can show trigger close, limit, observed segment, expiry, submit-to-fill ms, actual open and closed net result. Exact broker-deal evidence is required for pricing attribution; group completion also requires settlement of the whole pending book and closed broker positions. Old trades keep their original rules. Unfilled, expired and shadow plans are not realized P/L rows.

**Testing and interpretation:** `python -m pytest -q tests/test_hull_direct_retest.py` exercises the desk, native scanner and journal on fabricated MT5 inputs. In the controlled slow-profile test starting 100 ms after close, 600 ms charged per send placed 10 orders, the last at +5.5 s; 1.01 s per send placed 8, then stopped without retry. These are injected delays, **not measured MT5 latency**. Test on your feed by comparing selected → planned → acknowledged → actually filled, by pair, symbol and candle colour. Compare net result **per eligible signal**, including genuine no-fills, as well as net per filled trade. Separate unfinished or unresolved batches. A screenshot/Bid-bar range alone cannot reconstruct Ask, fill priority, latency, slippage, costs or profitability.
