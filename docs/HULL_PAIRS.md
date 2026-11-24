# Dynamic Hull: paired entries and exits

The active **Python demo desk** uses a parent for direction and exactly one smaller chart for timing. All four distinct charts are native MT5 bid charts. **M1 never triggers an entry or exit in this policy. M30 and M45 are not parents.**

| Owner (parent) | Required trigger close | Fast trigger exit for tickets it opened |
|---|---|---|
| M5 | M3 Hull cross | Verified opposing M3 Hull cross |
| M10 | M5 Hull cross | Verified opposing M5 Hull cross |
| M15 | M10 Hull cross | Verified opposing M10 Hull cross |

**Alternative entry route:** [Direct pullback limits — candle colour, price, expiry, batch latency and read-only setup](HULL_RETEST.md). `market_next_quote` remains the default. Retest limits **replace**, never supplement, the market batch.

## What qualifies

For a **BUY**, Hull B (built from closes) must actually cross above the selected Hull A on the **completed trigger candle**; the trigger chart's own closed structure and its own parent's last fully closed structure must both be bullish **at that close**. Mirror this for a **SELL**. Dynamic/16 uses the Low curve for bullish structure and the High curve for bearish structure; a switch of source is not itself a cross. A forming, stale, future, warming, neutral or missing structure cannot approve its pair. Neither a parent Hull cross nor an extra continuation BOS is required.

Native clocks need not line up. At an **11:36 M3 close**, M5 → M3 uses the M5 bar that **closed at 11:35**, not the forming M5 bar that will close at 11:40. When a parent and trigger close together, use the newly completed parent, not the previous bias. On shared boundaries the scanner waits within the original entry deadline for healthy co-due bars to publish; it does not pick whichever history RPC answers first, extend the deadline, or replay a missed close. An unavailable pair is not an eligible vote.

**One batch per symbol.** Judge only the two charts of each pair. E.g. an M10 BUY with bullish M10 and M15 can qualify for M15 even if M5 is bearish. If that same close also produces a qualifying M5 → M3 SELL, **skip both**. If multiple eligible pairs agree, choose the **highest parent** once. A winner vetoed by spread, risk or another desk check does not hand the signal to a lower parent. No duplicate, later parent-close add, partial-batch top-up or restart replay.

**MSS behavior stays with the parent.** An ordinary parent MSS closes its own tickets and skips its first otherwise-valid aligned trigger cross. If that cross is on the ordinary MSS close, it counts as the skip; the shift itself never opens a trade. If a counter-MSS never made a continuation BOS and a confirmed MSS restores the established trend, the first valid **later** trigger cross can qualify. No elapsed-bar limit, countertrend trade or new BOS requirement. A shift alone cannot enter. The child has to agree in direction; it does not impose a second first-cross skip.

**Default market entry and stop.** The first usable post-close bid/ask observed in the **immediate successor trigger candle** can price the batch (BUY at ask, SELL at bid). It is not a fill at the chart close or a guarantee of the next opening print. Freeze the SL below the **trigger cross candle’s low** for a BUY or above **its high** for a SELL. Cushion = max(one broker trade tick, trigger ATR × configured buffer + extra pips in price units), rounded outwards on the trade grid. Never substitute a swing, previous candle, parent wick or developing candle. If the frozen stop no longer fits, skip instead of widening it. Existing quote/entry maximum ages, one-tick drift bound, broker minimum distance, sessions/maintenance, funding, portfolio caps, per-ticket targets/ladder and demo authorization are unchanged. A tighter timeframe does not cap cash loss.

**Exits.** A verified opposing raw cross on the ticket's **own** trigger chart can request a close intrabar or at confirmation, even outside entry hours. It need not be an eligible reversal entry. An M3 flicker cannot close an M15 → M10 ticket. A shared chart retains its separate roles: e.g. an M10 cross can protect M15 tickets as their trigger and M10-owned tickets as their parent, but not M5-owned tickets. A child MSS alone is not a new fast-exit rule. Original **parent** opposite-cross/MSS and broker SL/TP protection remain. The manager records positions by reconciled identity, not a matching magic/comment alone. Software exits require the bot/terminal online, may miss ticks between polls, and are requests, not guaranteed fills.

## Switch safely in VS Code / PowerShell

1. **Wait until flat**, with no pending orders, unresolved sends or manager working a position. Do not stop an unknown/armed manager to upgrade. Back up your private YAML, scanner logs, home-directory state and journal checkpoint. In the same clean Windows repo root on `main`, apply the published fast-forward (or the supplied offline Git bundle). Do not reset/clean the checkout or overwrite your private files. A pull does not change a running Python process.
2. `config.demo.local.yaml` is a **full config**, not a template overlay. In its **existing `hull:` section**, change just these keys for all three pairs:

   ```yaml
   execution_mode: paired_structure
   eligible_timeframes: [M5, M10, M15]
   ```

   Use a subset only if intentional. These are **parents**; do not add M3 or M1. Keep `entry_timing: confirmed_cross_next_bar`, `indicator.structure_timeframe: null`, `indicator.stop: cross_candle`, your original magic, aliases, lots, sessions and risk limits. `--paired-confirm` selects the policy but cannot add a missing authorization; an incompatible list fails instead of silently expanding or narrowing it. An old private config without the mode key still has legacy timing when launched directly without an override. Do not use the old `--m1-confirm` launch command.
3. **Preflight and observe**, using exactly the profiles you intend. Review the explicit `--profile` lists in **both** PowerShell scripts; they can differ from YAML `enabled`. If those are your choices:

   ```powershell
   .\run_preflight.ps1
   # Inspect its report before continuing.
   .\run_arm_demo.ps1 -Observe
   ```

   Expect `READ ONLY`, `execution_mode: paired_structure`, `trigger_timeframe: per_parent`, `parent_to_trigger: {M5: M3, M10: M5, M15: M10}` (or your selected subset), `pair_history_ready: true` in preflight, `new_ticket_stop_policy: confirmed_pair_trigger_candle_wick_v1`, correct account, sizes, history and session mapping. `history_check` lists each chart once with both roles where applicable; readiness means loaded, current history, **not** that a pair is aligned. Review warnings. The observer only writes shadow decisions. Verify `checkout_head_at_start` and `source_module` in the latest `started` event. Stop that observer before any separately intended demo arming. **Running `run_arm_demo.ps1` without `-Observe` requests demo orders.**

   For other profiles use the same explicit `--profile` arguments with `hull-preflight --config config.demo.local.yaml --paired-confirm` and `trade-all --config config.demo.local.yaml --paired-confirm` (without `--arm-demo` for observation). Do not widen thresholds to make a preflight pass. For import/setup see the [root README](../README.md#start-safely-on-windows).
4. Do not switch managers with tickets open. Known M1/paired origins require their own manager, settings, selected parent and symbol. A paired start refuses unverified/legacy Hull exposure instead of assigning it a child. No private records need converting. Older [M1](HULL_M1.md) and [parent-close](HULL_STRUCTURE.md) policies are available for interpreting history, not additional lanes to run alongside this one. Wickless and MQ5/Pine/Free Chart studies are unchanged, not paired-fill displays.

## Follow one decision

Use the `audit_log` in `started.summary` (shadow or armed demo, same account hash). `pair_close_wait` documents staggered publication. `pair_arbitration` identifies the close, parent/trigger pair(s), both biases and timestamps, reasons, selected owner or conflict. `closed_label` identifies the selected signal; `label_decision` and `entry_quote_check` show admission/veto. `batch_preview` is **not a fill**. Per-leg `demo_result` records `parent_tf`, `trigger_tf`, both structure closes/biases, trigger wick/ATR, frozen SL, request, send quote and broker result. `close_to_detect_ms`, `close_to_submit_ms`, `order_send_roundtrip_ms` measure different intervals, not exchange latency. `pair_trigger_exit_signal`, `exit_request` and `lane_exit_confirmed` distinguish observation, requested close and confirmed flat.

New request tag: **`p15x10_<M10-close-seconds>_<leg>`** for M15 → M10 (likewise `p5x3` and `p10x5`). The journal keeps the parent in `timeframe`, the trigger in `trigger_timeframe`, `entry_timing: confirmed_pair_close` and the actual trigger candle's times. A tag can identify the pair; both-structure and wick/send evidence need an exact matching receipt. Only reconciled closed broker deals produce realized P/L. Existing `h…`, `m…`, `i…` or unknown trades are not reclassified. Keep the **same** journal output folder/checkpoint. [Reporter guide](../HullJournal/START-HERE.md).
