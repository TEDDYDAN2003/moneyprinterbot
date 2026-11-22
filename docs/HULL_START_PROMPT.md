# What to paste into your Windows desktop agent

**Approved Hull lanes: M15, M30, M45.** Verify the same list in the private config. A confirmed MSS exits only its own lane. These settings alone do not start or arm anything.

With the updated repository open in your agent's workspace, **send just this** for your three slow profiles:

```text
HULL START trio-slow
```

It means exactly:

```text
HULL START XAUUSD:0.09:10:slow EURUSD:0.01:10:slow GBPUSD:0.01:10:slow
```

On current `main`, slow requests **ten tickets**, not one. These explicit lots are per ticket; generic EURUSD/GBPUSD presets are larger. Verify the batch totals and `new_ticket_exit_policy: profit_ladder_70_30_v1` in preflight, plus `entry_policy: fc_dynamic_failed_mss_return_v3`, `entry_timing: live_hull_cross` and `choch_entry: never_standalone`. $10 is the **configured base** per new ticket. Its initial gross goal and ladder base also include that leg’s entry spread in USD. Preflight shows an indicative goal for each side.

To use different profiles, open [Hull demo start](Hull-demo-start.html), tick the instruments and select **Copy start line** or **Copy selected rows**. Only checked rows are included. The fields are **symbol, lot per ticket, base USD per ticket, rate**. No long prompt is necessary. These are messages for your agent, not shell commands.

The [README's standing startup instructions](../README.md#standing-instructions-for-the-desktop-agent) tell the agent to consult the HTML every time, verify the dedicated USD hedging demo terminal, run the tests and read-only preflight, then start one read-only observer on the approved timeframes with those exact profiles and report the log path. Only a separate, explicit **`ARM DEMO`** allows demo orders after rechecking. No live trading or changes to risk limits.

For an agent that does not automatically read repository instructions, send this once: **“Read `AGENTS.md` and the root `README.md` in this checkout. Follow their Hull demo startup procedure whenever I send selected profiles.”**
