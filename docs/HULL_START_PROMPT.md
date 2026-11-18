# What to paste into your Windows desktop agent

With the updated repository open in your agent's workspace, **send just this** for your three slow profiles:

```text
HULL START trio-slow
```

It means exactly:

```text
HULL START XAUUSD:0.09:10:slow EURUSD:0.01:10:slow GBPUSD:0.01:10:slow
```

To use different profiles, open [Hull demo start](Hull-demo-start.html), tick the instruments and select **Copy start line** or **Copy selected rows**. Only checked rows are included. The fields are **symbol, lot per ticket, TP USD per ticket, rate**. No long prompt is necessary. These are messages for your agent, not shell commands.

The [README's standing startup instructions](../README.md#standing-instructions-for-the-desktop-agent) tell the agent to consult the HTML every time, verify the dedicated USD hedging demo terminal, run the tests and read-only preflight, then start one read-only M1/M2/M5 observer with those exact profiles and report the log path. Only a separate, explicit **`ARM DEMO`** allows demo orders after rechecking. No live trading or changes to risk limits.

For an agent that does not automatically read repository instructions, send this once: **“Read `AGENTS.md` and the root `README.md` in this checkout. Follow their Hull demo startup procedure whenever I send selected profiles.”**
