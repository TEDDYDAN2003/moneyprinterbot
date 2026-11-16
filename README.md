# XAUUSDm 1-Minute Wickless Structure Trading Bot (MT5 + Telegram + Backtester)

A modular Python trading system for Gold (`XAUUSDm`) that translates your **Pine Script v6 `Wickless Structure Strategy`** into a tick-precision MetaTrader 5 Desktop execution bot with **multi-entry scaling**, **tick-monitored dollar TP/SL auto-closing** (e.g., close at `+$15.00` profit or a configured `$` loss), **Telegram remote control**, an **External Indicator Price Bridge**, and an **event-driven Backtester & Parameter Optimizer**.

---

## Key Capabilities

1. **Exact Port of Your `Wickless Structure Strategy` (M1)**:
   - Full `f_structure` engine: Internal (`intLen = 3`) & External (`extLen = 10`) swing pivots (`HH / HL / LH / LL`), Break of Structure (`BOS`), Market Structure Shift (`MSS / CHOCH`), protected high/low tracking, and `f_provLow` / `f_provHigh` provisional stop-loss placement.
   - Exact Wickless Candle detection (`tolTicks`, `minBodyTicks`) and all 3 entry modes:
     - `retest_limit` (tick-level precision entry right as live Ask/Bid touches `open ± zoneBuf`)
     - `retest_rejection` (zone touch + rejection candle close)
     - `immediate` (market execution on confirmed candle close)
   - Wilder RMA `ATR(14)` + mixed-swing consolidation context (optional filter for optimization).

2. **Structure Momentum Mode (`strategy.mode: "structure_momentum"`)**:
   - Builds confirmed M5 and M15 candles from completed M1 data and requires aligned structure.
   - Confirms BOS/CHOCH with candle closes, then evaluates M1 pullbacks, fair-value-gap zones, liquidity sweeps, MA slope, sessions, spread, volatility, and manual news windows.
   - Uses configurable tick-direction ratio confirmation before entry and records skipped-signal reasons.

3. **Tick-Monitored Dollar (`$`) Profit & Loss Auto-Closer**:
   - Monitors floating USD PnL on **every incoming tick** while keeping broker-side price SL/TP attached for crash/disconnect safety:
     - `trade_tp_usd`: Auto-close any individual trade once floating PnL hits `+$15.00` (configurable).
     - `trade_sl_usd`: Auto-close any individual trade at `-$X.XX`.
     - `trade_tp_usd_per_lot`: Optional lot-scaled target; `100.0` means 0.01 lot targets `$1` and 0.05 lot targets `$5`. When nonzero, it overrides `trade_tp_usd`.
     - `trade_sl_usd_per_lot`: Optional lot-scaled dollar stop; when zero, the strategy/broker stop remains the loss control.
     - `basket_tp_usd` / `basket_sl_usd`: Auto-close the entire stacked basket at a combined `+$` profit or `-$` loss.
     - `daily_max_profit_usd` / `daily_max_loss_usd`: Daily session circuit breakers.

3. **Multi-Entry Scaling with Hard Risk Caps**:
   - Stacks additional entries when aligned setups occur **only while existing trades are in profit** (`scale_only_in_profit: true`, zero martingale).
   - Hard limits on `max_open_positions`, `max_total_lots`, `max_total_risk_pct`, and `max_spread_points`.
   - Optional `max_trades_per_day`, `max_consecutive_losses`, and `loss_cooldown_minutes` controls pause new entries after excessive activity or a losing streak.
   - Profit-streak scaling can increase fixed lots gradually after closed wins, but every entry remains subject to `max_total_risk_pct`; `max_open_positions: 10` is only a ceiling, not a requirement to fill ten trades.

4. **External Indicator Price Bridge (`signals/external_signals.json` or TradingView Webhook)**:
   - Want a confirmed-close momentum setup? Set `strategy.mode: "structure_momentum"` to require M1 BOS/CHOCH, aligned completed M5/M15 structure, then a pullback/rejection and optional tick momentum confirmation. Session, spread, ATR/consolidation, and manually configured ISO news windows are supported; no external calendar data is assumed.
   - Want to feed `entry_price`, `sl_price`, and `tp_price` directly from TradingView alerts or an MT5 MQL5 indicator? Set `strategy.mode: "external_bridge"` or `"hybrid"`—the bot watches the live tick stream and executes as soon as market price reaches your indicator's configured price levels.

5. **Telegram Control Plane (Admin-Locked)**:
   - Start, pause, inspect M1 structure & positions, update `$` TP/SL targets, adjust risk %, or emergency-close everything directly from Telegram on your phone.

---

## Project Layout

- `bot/` — Core Python modules (`structure_engine.py`, `wickless_strategy.py`, `risk_manager.py`, `external_bridge.py`, `mt5_adapter.py`, `telegram_bot.py`, `engine.py`, `backtester.py`, `optimizer.py`)
- `scripts/` — CLI runners (`run_live.py`, `run_backtest.py`, `run_optimize.py`, `export_mt5_history.py`)
- `signals/` — Example JSON payload for the External Indicator Bridge
- `tests/` — Automated `unittest` suite
- `config.yaml` — Strategy, risk, multi-entry scaling, and `$` TP/SL configuration
- `.env.example` — Template for MT5 and Telegram secrets

---

## Manual Integration Guide (Windows MT5 Desktop + Telegram)

> **Important:** The official `MetaTrader5` Python package communicates locally with a running **MetaTrader 5 Desktop terminal on Windows**. Run the live bot on the same Windows PC/VPS where MT5 Desktop is open and logged in.

### Step 1: Install Python & Dependencies
Install Python 3.10+ (64-bit) on Windows, open PowerShell/CMD in this folder, and run:
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Step 2: Create Your Telegram Bot & Get Your Chat ID
1. Open Telegram and message **`@BotFather`** -> send `/newbot` -> copy the **Bot Token**.
2. Message **`@userinfobot`** -> copy your numeric **Chat ID** (so nobody else can control your bot).
3. Copy `.env.example` to `.env` and fill in your credentials:
   ```env
   MT5_SYMBOL="XAUUSDm"
   # Leave MT5_LOGIN empty to attach automatically to your currently open MT5 terminal
   MT5_LOGIN=""
   MT5_PASSWORD=""
   MT5_SERVER=""
   TELEGRAM_BOT_TOKEN="123456789:ABC..."
   TELEGRAM_ADMIN_CHAT_ID="987654321"
   ```

### Step 3: Configure MetaTrader 5 Desktop
1. Open MT5 Desktop and log into your broker account (e.g. Exness).
2. Open **Market Watch** (`Ctrl+M`) and ensure **`XAUUSDm`** (or your broker's Gold symbol) is visible.
3. Click **Tools -> Options -> Expert Advisors** and enable **Allow algorithmic trading** (and click the **Algo Trading** toolbar button so it is green).

### Step 4: Backtest & Optimize Before Going Live
1. **Export real `XAUUSDm` 1-minute history from MT5**:
   ```bash
   python scripts/export_mt5_history.py --bars 20000 --out data/xauusdm_m1.csv
   ```
2. **Run a backtest** (testing your Wickless Structure rules + `$15` TP / `$` SL controls):
   ```bash
   python scripts/run_backtest.py --csv data/xauusdm_m1.csv
   ```
   *(Or pull straight from MT5 without creating a CSV: `python scripts/run_backtest.py --from-mt5 --bars 10000`. Large requests are fetched in MT5-safe chunks.)*
3. **Run Walk-Forward Parameter Optimization** (uses Optuna TPE):
   ```bash
   python scripts/run_optimize.py --csv data/xauusdm_m1.csv --trials 40 --out config.optimized.yaml
   ```

### Step 5: Launch the Live Bot (Paper Dry-Run First, Then Live)
1. **Dry-run mode** (reads live MT5 ticks & structure and sends alerts to Telegram without placing real orders):
   ```bash
   python scripts/run_live.py --config config.yaml --dry-run
   ```
2. **Live execution mode**:
   ```bash
   python scripts/run_live.py --config config.yaml
   ```
3. In Telegram, send **`/run`** when you want the bot to begin executing trades (it starts in `PAUSED` safety state by default until you command `/run`).

### Current validation status

The current configuration was tested on 100,000 real MT5 M1 bars using a 240-point simulated spread. It produced 68 trades, a 1.495 profit factor, +$33.85 net PnL, and 6.99% maximum drawdown. This result uses synthetic four-point intrabar paths, so it is not a validated positive edge; keep live execution disabled until a tick-level, out-of-sample test is positive after spread and slippage.

---

## Telegram Commands Reference

| Command | Action |
|---|---|
| `/start` or `/help` | Show command menu |
| `/run` | Start reading M1 structure & ticks and executing valid setups |
| `/pause` | Pause new entries (active SL/TP & `$` exits keep guarding open trades) |
| `/status` | Account balance/equity, spread, M1 internal/external trend, active trades |
| `/closeall` | Immediately close all bot-managed trades and pause |
| `/set_tp_usd 15` | Close individual trades automatically when floating profit hits `+$15.00` |
| `/set_sl_usd 10` | Close individual trades automatically when floating loss hits `-$10.00` (`0` = off) |
| `/set_basket_tp_usd 15` | Close all stacked trades together at `+$15.00` combined profit |
| `/set_basket_sl_usd 30` | Close all stacked trades together at `-$30.00` combined loss |
| `/risk 1.0` | Set equity risk % per trade |
| `/lot 0.10` | Switch to fixed lot sizing |
| `/mode hybrid` | Switch between `wickless_structure`, `external_bridge`, or `hybrid` |
| `/params` | Print all active strategy & risk parameters |

---

## Integrating External TradingView / MQL5 Price Signals (Optional)

If you also want TradingView alerts from your Pine Script indicator (or an MQL5 script) to feed exact `entry_price`, `sl_price`, and `tp_price` levels into the bot:
1. Set `external_bridge.webhook_enabled: true` in `config.yaml` (or write JSON to `signals/external_signals.json` matching `signals/external_signals.example.json`).
2. Set `strategy.mode: "external_bridge"` or `"hybrid"`.
3. Send a POST payload (or write JSON) containing:
   ```json
   {
     "secret": "CHANGE_ME_SECRET",
     "symbol": "XAUUSDm",
     "side": "BUY",
     "entry_price": 3864.50,
     "sl_price": 3861.20,
     "tp_price": 3868.50,
     "tolerance_points": 15,
     "tag": "WicklessTV"
   }
   ```
   The bot will watch live MT5 ticks and execute automatically when the real market reaches your configured entry level.

---

## Disclaimer
Algorithmic trading on Gold (`XAUUSDm`) carries substantial risk of financial loss. Always validate parameters with historical backtests and run `--dry-run` or a demo MT5 account before trading live capital.
