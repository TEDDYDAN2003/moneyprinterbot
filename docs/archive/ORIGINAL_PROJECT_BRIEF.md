> **Historical project brief, not startup instructions.** For the active Hull scanner use the [repository README](../../README.md).

# AGENT PROMPT: XAUUSD Adaptive Multi-Mode Trading System (MetaTrader 5 + Python)

> Paste this whole document into your coding agent. Execute phases **in order**. Do not skip a phase's acceptance gate. Ask nothing unless blocked; make the stated defaults and record assumptions in `docs/ASSUMPTIONS.md`.

---

## 0. ROLE, MISSION, NON-NEGOTIABLES

**Role:** You are a senior quantitative engineer building a production-grade, institutional-style algorithmic trading system.

**Mission:** Build a Python system that trades **XAUUSD only** (design symbol-agnostic, train/enable only XAUUSD) through the MetaTrader 5 desktop terminal. The bot has **no single fixed strategy**. It reads the current price and a rich, multi-timeframe feature set, then lets calibrated models decide: *enter or skip, how many entries, hold or close, close early or not*.

**Targets (acceptance gates, not promises):** profit factor ≥ 1.5, max drawdown ≤ 10% of equity in out-of-sample tests, win rate ≥ 55% with average win/loss ratio ≥ 0.8, positive expectancy after costs. If a gate fails, report it honestly and iterate; never fake or leak data to pass.

**Non-negotiables**
1. **Everything tunable lives in `config.yaml`.** No magic numbers in code. Lot sizes, modes, thresholds, sessions, risk limits, model settings: all config.
2. **No lookahead / leakage.** Features at bar *t* use only data available at close of *t*. Multi-timeframe features use only *completed* higher-TF bars. Add automated leakage tests.
3. **Costs always modeled:** spread (variable), commission, slippage, in every backtest.
4. **Hard risk limits are code, not model outputs.** Models can never override the risk governor.
5. **Same code path for backtest, paper and live** (an `Execution` interface with `SimBroker` and `MT5Broker`).
6. **Fail safe:** on disconnect, stale ticks, or any unhandled error, stop opening trades and manage/close existing ones per `safety` config.
7. **Reproducible:** fixed seeds, versioned datasets/models, every run writes config snapshot + git hash + metrics.
8. Default `run.mode: paper`. Live requires explicit config change plus passing the Phase 14 checklist.

---

## 1. TECH STACK

- Python 3.11+, `MetaTrader5`, `pandas`, `numpy`, `numba`, `polars` (optional), `scikit-learn`, `lightgbm`, `optuna`, `river` (online learning), `shap`, `pydantic` (config validation), `pyyaml`, `pyarrow` (parquet), `loguru`, `pytest`, `hypothesis`, `matplotlib`/`plotly`, `typer` (CLI).
- Windows is required for MT5 terminal; code must import-guard `MetaTrader5` so backtests run on any OS.

---

## 2. REPOSITORY STRUCTURE (create exactly)

```
xauusd-bot/
├── config.yaml
├── pyproject.toml
├── README.md
├── docs/ (ASSUMPTIONS.md, ARCHITECTURE.md, RUNBOOK.md)
├── data/ (raw/, bars/, ticks/, features/, labels/)      # gitignored
├── models/ (registry/, champion/, challenger/)           # gitignored
├── reports/ (backtests/, walkforward/, live/)
├── src/bot/
│   ├── config/        schema.py, loader.py
│   ├── data/          mt5_client.py, downloader.py, tick_store.py, bar_builder.py, calendar.py, quality.py
│   ├── time/          sessions.py, clock.py
│   ├── features/      trend.py, structure.py, levels.py, pullback.py, momentum.py,
│   │                  volatility.py, microstructure.py, news.py, pipeline.py
│   ├── labeling/      triple_barrier.py, level_reaction.py, exit_labels.py, mae_mfe.py
│   ├── models/        entry_model.py, level_model.py, exit_model.py, loss_guard.py,
│   │                  calibration.py, registry.py, online.py, drift.py
│   ├── decision/      mode_engine.py, batch_planner.py, rr_weigher.py, signal_fusion.py
│   ├── risk/          sizing.py, streak.py, governor.py, cooldown.py
│   ├── execution/     broker_base.py, sim_broker.py, mt5_broker.py, order_manager.py
│   ├── management/    trade_manager.py, profit_lock.py, early_exit.py, news_reentry.py
│   ├── backtest/      engine.py, costs.py, walkforward.py, montecarlo.py, metrics.py
│   ├── training/      dataset.py, train_entry.py, train_exit.py, tune.py, retrain_scheduler.py
│   ├── live/          runner.py, state.py, healthcheck.py
│   └── cli.py
└── tests/ (unit/, integration/, leakage/, property/)
```

---

## 3. `config.yaml` (generate this file in full, with comments; validate with pydantic)

```yaml
run:
  mode: paper                 # backtest | paper | live
  symbol: XAUUSD
  broker_symbol_suffix: ""    # e.g. "m" or ".pro" if your broker names it differently
  timezone_broker: "Etc/GMT-2"   # broker server tz; verify in Phase 1
  seed: 42
  log_level: INFO

mt5:
  terminal_path: ""           # blank = auto-detect
  login: null
  password_env: MT5_PASSWORD  # read from env var, never hardcode
  server: ""
  magic_number: 20260101
  deviation_points: 20        # max slippage on order send
  filling_mode: auto          # auto | ioc | fok | return

data:
  history_years: 3
  base_timeframe: M1
  timeframes: [M1, M3, M5, M10, M15]
  store_ticks: true
  tick_retention_days: 120
  quality:
    max_gap_minutes: 5
    drop_weekend_gaps: true
    outlier_zscore: 12

sessions:                      # all times UTC; DST handled by tz-aware definitions below
  asia:        {start: "00:00", end: "07:00", tz: UTC}
  london:      {start: "08:00", end: "16:00", tz: Europe/London}
  new_york:    {start: "08:00", end: "17:00", tz: America/New_York}
  killzones:
    london_open:   {start: "07:00", end: "10:00", tz: Europe/London}
    ny_open:       {start: "07:00", end: "10:00", tz: America/New_York}
    london_ny_overlap: {start: "13:00", end: "16:00", tz: Europe/London}
  trade_only_in: [london_open, ny_open, london_ny_overlap]   # [] = trade any time
  allow_asia: false
  rollover_blackout: {start: "21:55", end: "23:05", tz: UTC}

timeframe_roles:
  entry_tf: [M1, M3, M5, M10, M15] # one bot per entry timeframe
  small_tf: [M1, M3, M5]           # internal structure
  medium_tf: [M10, M15]
  large_tf: [H1, H4, D1]       # external trend and major levels

modes:
  active: auto                 # fast | medium | slow | auto (engine picks)
  fast:
    batch_size: [6, 7]         # min,max entries per signal
    batch_spacing_seconds: [1, 4]
    tp_atr_mult: 0.35
    sl_atr_mult: 0.70
    max_hold_seconds: 180
    min_entry_prob: 0.60
    min_momentum_score: 0.70
  medium:
    batch_size: [3, 4]
    batch_spacing_seconds: [3, 10]
    tp_atr_mult: 0.80
    sl_atr_mult: 1.00
    max_hold_seconds: 600
    min_entry_prob: 0.64
    min_momentum_score: 0.55
  slow:
    batch_size: [1, 1]
    batch_spacing_seconds: [0, 0]
    tp_atr_mult: 1.80
    sl_atr_mult: 1.20
    max_hold_seconds: 3600
    min_entry_prob: 0.68
    min_momentum_score: 0.40
  auto_selector:
    fast_if:   {session_in: [london_open, ny_open], momentum_score_gte: 0.75, spread_pctile_lte: 0.6, atr_pctile_gte: 0.4}
    slow_if:   {near_major_level: true, or_atr_pctile_lte: 0.25}
    else: medium

lots:                           # YOU ADJUST THESE
  mode: fixed                   # fixed | risk_pct
  fixed:
    fast_lot_per_trade: 0.01
    medium_lot_per_trade: 0.02
    slow_lot_per_trade: 0.05
  risk_pct:
    per_batch_risk_pct: 0.5     # total risk of a whole batch as % equity
  min_lot: 0.01
  max_lot_per_trade: 0.50
  max_total_lots_open: 1.00
  lot_step: 0.01
  streak_scaling:
    enabled: true
    win_step_multiplier: 1.15   # per consecutive winning batch
    max_multiplier: 1.75        # hard cap on reward scaling
    loss_step_multiplier: 0.75  # per consecutive losing batch (penalty)
    min_multiplier: 0.40
    reset_after_minutes_flat: 30

risk:
  max_open_trades: 12
  max_open_trades_per_direction: 8
  max_daily_loss_pct: 2.0
  max_daily_profit_lock_pct: 4.0      # after this, only protect gains (no new batches)
  max_weekly_drawdown_pct: 5.0
  max_equity_drawdown_kill_pct: 8.0   # kill switch: flatten and halt
  max_spread_points: 35
  max_slippage_points: 25
  news_blackout_minutes: {before: 2, after: 0}   # overridden by news mode if enabled
  min_free_margin_pct: 60

cooldown:
  enabled: true
  trigger_consecutive_losing_batches: 2
  seconds: 30
  during_cooldown: reorganize        # recompute levels, features, recalibrate thresholds
  escalate: {after_cooldowns_in_hour: 3, seconds: 180}

levels:
  swing_lookback: {small: 5, medium: 8, large: 12}   # fractal pivot strength per TF group
  cluster_tolerance_atr: 0.25
  min_touches: 2
  fib_pullbacks: [0.382, 0.5, 0.618, 0.786]
  round_numbers: {step: 5.0, major_step: 25.0}       # gold $5 / $25 levels
  proximity_atr: 0.35                                # "at level" definition
  recency_decay_hours: 72
  decision:                                          # break-vs-reject weighing
    min_reject_prob: 0.62
    min_break_prob: 0.62
    min_reward_to_risk: 1.5
    allow_hold_for_day_extreme: true

momentum:
  windows_seconds: [5, 15, 30, 60]
  windows_bars: [3, 5, 10]
  score_weights: {body_ratio: 0.2, consecutive: 0.15, roc: 0.2, tick_velocity: 0.2, range_expansion: 0.15, volume_surge: 0.1}
  decay_exit_threshold: 0.35        # momentum below this while in profit => harvest
  burst_zscore: 2.5                 # "irregular momentum" detector

news:
  enabled: true
  calendar_source: "forexfactory_csv_or_api"
  impact_filter: [high]
  currencies: [USD]
  pre_window_minutes: 2
  post_window_minutes: 20
  mode: momentum_burst              # avoid | momentum_burst
  reentry:
    max_entries: 4
    require_prior_trade_profit: true
    stop_on_momentum_decay: true
    spread_max_points: 60

trade_management:
  breakeven: {enabled: true, trigger_r: 0.5, offset_points: 3}
  partial_close: {enabled: true, at_r: [0.6, 1.0], fractions: [0.5, 0.25]}
  trailing: {enabled: true, type: atr_chandelier, atr_mult: 1.0, start_r: 0.8}
  profit_giveback_pct_max: 35       # close if open profit retraces this % from peak
  harvest_instantly_in_killzone: true
  harvest_min_r: 0.35
  early_exit:
    enabled: true
    loss_guard_prob_threshold: 0.70   # P(trade ends at SL | state) above this => close now
    check_interval_seconds: 1
    min_age_seconds: 4
    max_adverse_r_without_progress: 0.45
    stale_trade_seconds: {fast: 90, medium: 300, slow: 1800}
  hold_for_extension:
    enabled: true
    require: [trend_aligned_large_tf, momentum_score_gte_0.6, break_prob_gte_0.65]

models:
  entry:
    algo: lightgbm
    objective: binary
    calibration: isotonic
    params: {num_leaves: 31, learning_rate: 0.03, n_estimators: 800, min_child_samples: 200, subsample: 0.8, colsample_bytree: 0.7, reg_lambda: 5.0}
  level_reaction:
    algo: lightgbm
    classes: [reject, break, chop]
  exit_hold_vs_close:
    algo: lightgbm
  loss_guard:
    algo: lightgbm
    horizon_seconds: [10, 30, 60]
  baseline_logreg: true
  regime: {algo: hmm, n_states: 3}
  explain: {shap: true, top_k: 25}

labeling:
  triple_barrier: {use_mode_tp_sl: true, vertical_barrier_from_mode: true}
  sample_weighting: uniqueness_and_return
  min_samples_per_session: 3000

training:
  initial_years: 3
  validation: purged_walk_forward
  train_window_days: 180
  test_window_days: 30
  embargo_minutes: 60
  optuna: {n_trials: 150, direction: maximize, objective: calmar_after_costs, pruner: median}
  param_stability_check: true
  online:
    enabled: true
    ingest: tick_to_bar_stream
    micro_update_every_minutes: 15     # river online learner adjusts, bounded
    full_retrain: {schedule: "sat 08:00 UTC", trigger_on_drift: true}
    drift_detectors: [adwin, psi]
    promotion: {champion_challenger: true, min_oos_days: 10, must_beat_pf_by: 0.05, must_not_worsen_dd: true}
    max_model_age_days: 14

backtest:
  initial_balance: 10000
  commission_per_lot: 7.0
  spread_model: empirical_by_hour     # learned from data
  slippage_model: {type: proportional_to_atr, factor: 0.02}
  latency_ms: 150
  intrabar_resolution: tick_if_available_else_m1_conservative

safety:
  stale_tick_seconds: 5
  disconnect_action: halt_new_and_manage
  max_orders_per_minute: 40
  heartbeat_seconds: 2
  require_sl_on_every_order: true
  emergency_flatten_hotkey_file: "KILL"   # creating this file flattens and halts

reporting:
  telegram: {enabled: false, token_env: TG_TOKEN, chat_id_env: TG_CHAT}
  daily_report: true
```

---

## 4. PHASES (execute sequentially; each has an ACCEPTANCE GATE)

### PHASE 1: Scaffold and Config
- Create the repo tree, `pyproject.toml`, pydantic schema mirroring every config key, loader with env-var secrets, CLI skeleton (`bot download | features | label | train | tune | backtest | walkforward | paper | live | report`).
- **Gate:** `pytest` passes; invalid config produces a clear error; config snapshot saved per run.

### PHASE 2: MT5 Data Layer
1. `mt5_client.py`: connect, symbol check (`symbol_select`), fetch symbol specs (digits, point, tick size/value, contract size, volume min/max/step, stops level, spread).
2. Download `history_years` of M1 (and build M2..D1 by resampling, with completed-bar rules) plus ticks (`copy_ticks_range`) in monthly chunks, resumable, stored as parquet.
3. **Detect broker server timezone and DST offset** by comparing to a known session open; convert everything to UTC.
4. `quality.py`: gap detection, duplicate removal, outlier filtering, weekend handling, report in `reports/data_quality.md`.
5. `calendar.py`: ingest high-impact USD news (CSV/API), store UTC timestamps.
6. Build an empirical **spread-by-hour-and-session** table from ticks for the cost model.
- **Gate:** data-quality report generated; timezone verified with an assertion test; no future-timestamp rows.

### PHASE 3: Time, Sessions, Kill Zones
- `sessions.py` with tz-aware DST-correct session flags: asia, london, new_york, each killzone, overlap, minutes-since-session-open, minutes-to-session-close, day-of-week, rollover blackout.
- **Gate:** unit tests across DST transitions in both London and New York.

### PHASE 4: Multi-Timeframe Feature Engine (`features/`)
All features computed per decision timestamp across small/medium/large TF groups and exposed via `pipeline.py` (vectorized, cached, identical in live streaming).

**4a. Trend (core feature, highest weight by design through learning, not hand-set)**
- EMA(9/21/50/200) slopes and relative distance (ATR-normalized), ADX/DI, higher-high/higher-low counts, linear regression slope and R², Kaufman efficiency ratio.
- Per-TF trend state ∈ {-1,0,+1} and a **multi-TF alignment score** (small vs medium vs large). "Internal trend" (M1/M5) vs "external trend" (H1/H4/D1) agreement and divergence flags.
- Regime probabilities from the HMM (trend / range / volatile).

**4b. Structure**
- Swing pivots (fractals) per TF group, BOS/CHOCH events, FVG presence/age/fill ratio, order blocks (last opposing candle before displacement), previous day/week high-low, session highs/lows (Asia range, London range), distance to each in ATR units.

**4c. Pullback engine (`pullback.py`)** *(crucial)*
- Identify the dominant leg (impulse) on each TF; compute retracement depth as % of the leg and in ATR; fib zone hit (0.382/0.5/0.618/0.786); pullback duration and its momentum character (corrective = overlapping, low-volume, decaying; reversal = impulsive counter-move).
- Outputs: `pullback_depth`, `pullback_is_healthy`, `pullback_resolved_prob` (model), `with_trend_entry_zone` flag.
- **Rule encoded as features/guards:** do not sell into a healthy pullback end in an uptrend (and vice versa) unless the level model says reject with high confidence and reward/risk passes.

**4d. Support/Resistance level engine (`levels.py`)** *(crucial)*
- Build a **level map** from small, medium and large TFs: clustered swing highs/lows (tolerance in ATR), touch count, recency decay, rejection wick strength, volume at level, round-number levels, prior-day/week extremes, fib levels, order-block edges.
- Merge levels across TFs into **zones** with a **strength score** (higher-TF levels weigh more). Maintain the map incrementally on each completed bar.
- Per decision: nearest support/resistance above/below, distance (ATR), strength, count of TFs confluent, whether price is "at level" (`proximity_atr`), approach momentum, and whether the level has recently been tested.

**4e. Momentum (`momentum.py`)** *(drives instant entries in kill zones)*
- Candle: body/range ratio, close location in range, consecutive same-direction candles, wick rejection ratio, range expansion vs ATR, engulfing/displacement flags.
- Tick-level: tick velocity (ticks/sec and points/sec) over `windows_seconds`, signed order-flow proxy (tick-rule buy/sell imbalance), acceleration (change in velocity), spread behavior.
- **Composite `momentum_score` ∈ [0,1]** using `momentum.score_weights`, but final weighting comes from the model, the config weights are only a transparent baseline.
- **Burst detector:** z-score of velocity vs rolling session-specific distribution, flags irregular momentum changes (`burst_zscore`).
- **Decay detector:** momentum slope turning negative, wick growth, velocity falling, used by exits.

**4f. Volatility/Microstructure:** ATR (multiple TFs) and its percentile, realized vol, Parkinson/Garman-Klass, spread percentile vs session norm, tick arrival rate, spread spike flag.

**4g. News (`news.py`):** minutes to/from next high-impact event, event surprise magnitude (if available), post-news realized volatility regime, `in_news_window`.

**4h. Normalization:** express price features in ATR units; rolling z-scores per session so "average values" are learned per time-of-day (this is the data-driven version of "assign average values and detect shifts").

- **Gate:** leakage test suite passes (features at *t* unchanged when future data is perturbed); feature pipeline runtime ≤ 20 ms per decision in streaming mode; feature parity test between batch and streaming outputs.

### PHASE 5: Labeling (`labeling/`)
1. **Triple-barrier labels per mode** using that mode's `tp_atr_mult`, `sl_atr_mult`, `max_hold_seconds`, evaluated on tick data where available (conservative on M1: if both TP and SL are inside one bar, assume SL first).
2. **MAE/MFE** per candidate trade: max adverse/favorable excursion path sampled at 1s/5s/15s/30s/60s.
3. **Level-reaction labels:** when price is "at level", label next N minutes as `reject` / `break` (close beyond level by X ATR and holds) / `chop`.
4. **Exit labels:**
   - *Loss-guard label:* at time *k* after entry with the trade in drawdown, did it eventually hit SL (1) or recover to ≥ breakeven (0)?
   - *Hold-vs-close label:* at time *k* with the trade in profit, did holding add expected R (final R − current R > 0)?
5. Sample weights by label uniqueness and return magnitude; purge/embargo overlaps.
- **Gate:** label distribution report per session and mode; no overlap leakage; class balance documented.

### PHASE 6: Models (`models/`)
1. **Baseline:** logistic regression (coefficients exported as a readable "weights" table).
2. **Entry model (LightGBM):** P(trade reaches TP before SL) per direction per mode. Calibrate (isotonic) and verify with reliability curves, Brier score.
3. **Level model:** 3-class reject/break/chop at levels.
4. **Loss-guard model (early-loss predictor):** inputs = entry features + live trade state (age, current R, MAE so far, momentum now vs at entry, adverse velocity, distance to nearest level against us, spread, session phase). Output = P(final loss | state) at each horizon. **If above `loss_guard_prob_threshold`, close immediately.** This is how the bot "estimates that a losing entry will lose further."
5. **Profit-hold model:** P(holding adds R) to decide hold vs harvest, combined with momentum decay and giveback rules.
6. **Regime model:** HMM, output used as features and for mode selection.
7. **Explainability:** SHAP summary per session; export `reports/feature_weights.md` so the user sees which features matter in each session.
8. **Registry:** versioned models with metadata (data range, config hash, metrics); champion/challenger folders.
- **Gate:** OOS AUC > 0.55 and calibrated probabilities (ECE < 0.05) on purged walk-forward folds; baseline vs LightGBM comparison table; models beating coin-flip with stability across folds.

### PHASE 7: Decision Engine (`decision/`)
**Signal fusion (`signal_fusion.py`)**: no fixed strategy. At each decision tick, for each direction compute:
`score = f(entry_prob, trend_alignment, pullback_state, level_state, momentum_score, session_quality, regime, spread_ok)`, learned, not hand-coded; hard filters (spread, blackout, risk governor) are applied first.

**Context rules the engine must respect (implemented as features + guards):**
- **Trade with the trend by default:** counter-trend entries are allowed only at strong multi-TF levels with `reject_prob ≥ min_reject_prob` and `reward_to_risk ≥ min_reward_to_risk`.
- **Never fade a healthy pullback's end** against the main trend.
- **At resistance (for sells) / support (for buys)** consult the level model: if `reject` is dominant, enter with a tight stop beyond the level; if `break` is dominant, do **not** fade, and consider entering in the breakout direction after confirmation (close beyond level + retest or displacement).

**Risk/reward weigher (`rr_weigher.py`)**: for each candidate, compute expected value:
`EV = P(win)*avg_win_R − P(loss)*avg_loss_R − costs_R`, using calibrated probabilities and level-derived targets (next opposing level) and stops (beyond invalidation). Require EV > `min_ev` and R:R ≥ configured minimum. For the "sell the high of the day" scenario: if `allow_hold_for_day_extreme` and conditions (reject prob high, large-TF exhaustion, momentum decay into level) hold, allow a **runner** (partial profit taken, remainder held with a trailing stop). The EV weigher must show why the runner is justified in the trade log.

**Mode engine (`mode_engine.py`)**: choose fast/medium/slow via `modes.auto_selector` or fixed override. Hard rule: mode selection must be **fast to qualify**. Avoid the old failure of too many conjunctive conditions: the entry gate is a **single calibrated probability threshold + hard safety filters**, not 10 required conditions. Log the "time since last signal" metric; if fast mode produces < N signals per kill-zone hour in backtest, raise an automated warning and suggest threshold relaxation via Optuna.

**Batch planner (`batch_planner.py`)**:
- Fast: 6-7 entries, medium: 3-4, slow: 1 (from config ranges; actual size scaled by confidence and by remaining `max_open_trades`).
- Entries are staggered per `batch_spacing_seconds`; **each subsequent entry in a batch re-checks** momentum and entry probability; abort the remainder if momentum fades or the first entries are immediately adverse.
- Each order has its own TP/SL; fast-mode orders may use laddered TPs (e.g., 0.25/0.35/0.5 ATR) so some close instantly and the rest ride.

### PHASE 8: Risk, Sizing, Streaks, Cooldown (`risk/`)
- **Sizing:** `lots.mode` fixed or risk_pct (compute lot from SL distance, tick value, per-batch risk, then split across batch entries), clamp by `min_lot`, `max_lot_per_trade`, `max_total_lots_open`, margin check, round to `lot_step`.
- **Streak engine (anti-martingale only):** on consecutive winning batches multiply lot by `win_step_multiplier` up to `max_multiplier`; on consecutive losing batches multiply by `loss_step_multiplier` down to `min_multiplier`. **Never increase size after losses.** Reset after `reset_after_minutes_flat`.
- **Cooldown:** after `trigger_consecutive_losing_batches` losing batches: pause new entries for `cooldown.seconds` (30 s default). During cooldown: keep managing open trades, rebuild the level map, refresh features, check spread/regime, and re-evaluate the mode. Escalate the pause if repeated.
- **Governor (`governor.py`):** enforce all `risk.*` limits, daily loss limit, profit lock (after `max_daily_profit_lock_pct`, new batches are disallowed or size-reduced), weekly DD, equity kill switch, exposure limits, spread/slippage limits. Governor decisions are logged with reason codes.
- **Gate:** property-based tests (hypothesis) proving no sequence of signals can violate exposure/loss limits.

### PHASE 9: Trade Management (`management/`)
Evaluated every `check_interval_seconds` per open trade (and on every tick for fast mode):
1. **Instant harvest in kill zones:** if `harvest_instantly_in_killzone` and open profit ≥ `harvest_min_r`, and momentum decay or hold-model says "no extra expected R", **close now** (take the profit).
2. **Momentum-decay exit:** in profit and `momentum_score < decay_exit_threshold` or velocity reversal → close.
3. **Profit giveback guard:** close if profit retraces `profit_giveback_pct_max` from peak.
4. **Breakeven / partials / ATR trailing** per config.
5. **Early loss exit (`early_exit.py`):** use the loss-guard model plus hard rules (adverse excursion beyond `max_adverse_r_without_progress` with no favorable progress after `min_age_seconds`, adverse momentum aligned with higher-TF trend, level broken against us) → close before the SL.
6. **Stale-trade exit:** time stop per mode if no progress.
7. **Hold-for-extension:** only when `hold_for_extension.require` conditions hold (large-TF aligned, strong momentum, high break probability); then trail and let it run.
8. **News mode (`news_reentry.py`):** around high-impact events with `mode: momentum_burst`: enter on detected bursts in the burst direction (small size), and **re-enter after each profitable harvest** (up to `reentry.max_entries`) while momentum persists; stop immediately on decay, spread blowout or first loss.
- **Gate:** simulated scenarios (unit tests with synthetic price paths) for each exit rule; trade log shows the reason code for every close.

### PHASE 10: Execution Layer (`execution/`)
- `broker_base.py` interface: `send`, `close`, `modify`, `positions`, `account`, `tick`.
- `sim_broker.py`: tick/M1 fills with variable spread, slippage, latency, commission, stop-level checks, margin.
- `mt5_broker.py`: `order_send` with deviation, filling mode autodetect, retries with backoff, error-code handling (requotes, off quotes, invalid stops, no money), **always attach SL**, magic number tagging, position reconciliation on restart.
- Rate limiter (`max_orders_per_minute`), idempotent order IDs, heartbeat, stale-tick detection.
- **Gate:** integration test where SimBroker and a mocked MT5 return identical lifecycle events.

### PHASE 11: Backtest Engine and Metrics (`backtest/`)
- Event-driven engine using the exact live decision path; tick-resolution when ticks exist; conservative M1 intrabar assumptions otherwise.
- Metrics: net profit, profit factor, win rate, avg win/loss, expectancy (R), max & average drawdown, Calmar, Sharpe/Sortino, recovery factor, trades/hour in each session, time-in-trade, per-mode/per-session/per-exit-reason breakdown, and **cost drag**.
- **Monte Carlo** (trade reshuffle + block bootstrap, 5,000 runs): 95th-percentile max drawdown, probability of ruin.
- **Gate:** reconciliation test (a hand-computed small scenario matches engine PnL to the cent).

### PHASE 12: Training, Tuning, Walk-Forward (`training/`)
1. Build datasets (features + labels) per mode; store to parquet with version hash.
2. **Purged walk-forward** (`train_window_days`, `test_window_days`, `embargo_minutes`); no shuffling.
3. **Optuna tuning** of thresholds (`min_entry_prob`, TP/SL ATR multiples, trailing params, harvest/giveback params, cooldown, level proximity) maximizing `calmar_after_costs` on training folds only, evaluated on the untouched following fold.
4. **Stability check:** reject parameter sets lying on sharp peaks; prefer plateaus (neighbor perturbation ±10% must retain ≥ 80% of the objective).
5. Per-session analysis: train one model with session features; train session-specific models only where `min_samples_per_session` is satisfied; otherwise keep the shared model.
6. Final holdout (last 2-3 months) used **once**; report separately.
- **Gate:** walk-forward report shows each fold's PF, DD, win rate; aggregate OOS meets targets or report states honestly which gate failed and why.

### PHASE 13: Continuous / Online Learning (answering "how do I train as ticks keep pouring in?")
Implement this exact design in `live/` and `training/online.py`:
1. **Ingestion:** a tick collector thread writes ticks to parquet partitions and a ring buffer; a bar builder produces completed bars and updates features incrementally (same code as batch).
2. **Never train on every tick.** Train on **completed decision samples** (feature vector at decision time + outcome after the horizon closes). Labels are created once outcomes mature (delayed labeling queue).
3. **Three update layers:**
   - *Layer A, online micro-adaptation (every `micro_update_every_minutes`)*: `river` incremental learner or calibration-layer update (e.g., online isotonic/Platt) adjusting probability **calibration and thresholds** within bounded ranges. It does not replace the core model.
   - *Layer B, scheduled full retrain (weekly, `sat 08:00 UTC`)*: retrain LightGBM models on a rolling window (e.g., last 180 days, recency-weighted) via the Phase 12 pipeline.
   - *Layer C, drift-triggered retrain*: ADWIN on prediction error / PSI on key features; trigger a challenger retrain when drift is detected.
4. **Champion/challenger promotion:** the challenger shadow-trades (paper) alongside the champion for ≥ `min_oos_days`; promote only if PF improves by ≥ `must_beat_pf_by` and DD doesn't worsen; keep rollback to the previous champion with one command.
5. **Safeguards:** cap how far online adaptation may move thresholds; freeze adaptation during news windows and after the kill switch; log every model/threshold change.
- **Gate:** simulation replaying 6 months as a stream demonstrates delayed labeling, a retrain, a promotion decision and a rollback, with no leakage (label timestamps strictly earlier than the training cutoff).

### PHASE 14: Live Runner, Safety, Monitoring (`live/`)
- Main loop: tick → feature update → governor → decision engine → batch planner → execution → management → logging; target decision latency < 100 ms for fast mode.
- Health checks: MT5 connectivity, tick staleness, clock drift, model age, free margin; heartbeat file; auto-reconnect; `KILL` file support; graceful shutdown flattens or manages per config.
- Structured logs (JSON): every decision with feature snapshot hash, probabilities, mode, EV calculation, risk-governor outcome, and exit reason codes. Daily report (PNG equity curve, session/mode breakdown, top feature drift), optional Telegram alerts.
- **Pre-live checklist (`docs/RUNBOOK.md`):** ≥ 2 weeks of paper trading matching backtest within tolerance (PF within 20%, slippage and spread assumptions verified), kill switch tested, lot limits reviewed by the user, then switch `run.mode: live` with the minimum lot sizes first.

### PHASE 15: Testing and Quality
- Unit tests for features, labels, sizing, governor, exit rules; **leakage tests**; property tests for risk invariants; backtest-vs-live parity test; type hints with `mypy --strict` on `risk/` and `execution/`; `ruff`; CI script.

### PHASE 16: Documentation and Final Report
- `README.md` with quick start (`bot download → features → label → train → tune → walkforward → backtest → paper → live`), `ARCHITECTURE.md` (diagram of data flow), `ASSUMPTIONS.md`, and a **final performance report** with tables per mode/session/exit reason, honest limitations, and recommended next steps.

---

## 5. DESIGN PRINCIPLES TO ENFORCE

1. **Few strong gates, not many conjunctive conditions.** One calibrated probability and hard safety filters decide entries. Features inform the model; they are not all required to align.
2. **Rewards scale up slowly, penalties scale down fast.** Anti-martingale only.
3. **Exits matter as much as entries.** Most drawdown reduction comes from the loss guard, profit harvest and risk governor.
4. **Every close has a reason code;** every parameter has a config key.
5. **Session-aware normalization:** "average values" are learned per session and time of day.
6. **Honesty over optimism:** report failing gates plainly; no cherry-picked periods.

## 6. DELIVERY ORDER FOR THE AGENT

Work phase by phase. After each phase: run tests, print the gate result, commit with message `phaseN: <summary>`, then continue. At the end output: (1) the file tree, (2) the gate summary table, (3) the exact commands to run download → train → backtest → paper, (4) a list of every `config.yaml` key the user will most likely tune (lot sizes first).

**Begin with Phase 1 now.**