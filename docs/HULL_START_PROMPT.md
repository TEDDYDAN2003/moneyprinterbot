# Paste into the AI agent on your Windows MT5 machine

Replace the example profiles before using. Format: `SYMBOL:LOTS_PER_POSITION:TP_USD_PER_POSITION:RATE`. `slow` = 1, `medium` = 3–4, `fast` = 6–7. Only list the symbols you want the bot to observe or trade.

```text
Connect the Moneyprinterbot Hull-label scanner to my running MT5 Desktop. DEMO ONLY.
Use M1, M2 and M5. My profiles are XAUUSD:0.09:10:fast and EURUSD:0.01:10:slow.

Use the updated repository, from its root. Check that the Hull commands exist. Install the project's test dependencies, MetaTrader5 and tzdata in the local Python environment if needed. Do not ask me to send broker or Git credentials. Verify the attached terminal is my dedicated USD hedging demo, not live, contest or netting. Check that no older trading process is running. Do not close manual positions.

Run tests, then run `python -m bot.cli hull-preflight --profile XAUUSD:0.09:10:fast --profile EURUSD:0.01:10:slow` (substitute my actual profiles in every command). Show me the exact resolved broker symbols, timeframes, per-ticket lots and TP, min/max batch lots and target, existing exposure, latest historical label/stop anchor, permissions and account risk limits. If a broker symbol or lot is invalid or ambiguous, stop and ask me; do not guess a substitute. Do not raise my account risk limit to force a batch.

Use raw closed-bar Hull BUY/SELL labels, not the EMA-filtered simulator. Buy: SL under the preceding local pullback low. Sell: SL over the preceding local pullback high. Keep the configured ATR buffer. The target is per ticket, not shared by the batch. No momentum holds or M15 worker. Start in read-only mode using `python -m bot.cli trade-all` with my profiles. Show the log path. Do not send an order merely to test connectivity.

Only when I explicitly say ARM DEMO: stop the read-only instance, use an ignored local copy config.demo.local.yaml with run.mode=demo, verify permissions, and start one `python -m bot.cli trade-all --config config.demo.local.yaml --arm-demo` process with the same --profile arguments. Never enable live orders or leave multiple armed processes running. Report skipped signals, batch risk, fills and broker TP/SL; leave all account safeguards in place.
```

Short repeat instruction after installation: **“Start the M1/M2/M5 Hull observer on my USD MT5 demo. Profiles: `XAUUSD:0.09:10:slow`. Preflight and report; do not arm until I say ARM DEMO.”**
