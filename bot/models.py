from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal, Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self == Side.BUY else -1


class BotRunState(str, Enum):
    PAUSED = "PAUSED"
    RUNNING = "RUNNING"
    HALTED_DAILY_PROFIT = "HALTED_DAILY_PROFIT"
    HALTED_DAILY_LOSS = "HALTED_DAILY_LOSS"


@dataclass(frozen=True)
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = 0
    spread: int = 0


@dataclass(frozen=True)
class Tick:
    time: datetime
    bid: float
    ask: float
    last: float = 0.0
    volume: float = 0.0
    time_msc: int = 0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    def spread_points(self, point: float) -> float:
        if point <= 0:
            return 0.0
        return (self.ask - self.bid) / point


@dataclass(frozen=True)
class SymbolSpec:
    symbol: str
    point: float
    digits: int
    trade_tick_size: float
    trade_tick_value: float
    trade_contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int = 0
    filling_mode: int = 1  # MT5 filling mode bitmask or resolved constant


@dataclass
class StructureSnapshot:
    bar_index: int
    internal_trend: int  # 1 = bullish, -1 = bearish, 0 = neutral
    external_trend: int
    last_hh: Optional[float] = None
    last_hl: Optional[float] = None
    last_ll: Optional[float] = None
    last_lh: Optional[float] = None
    last_high: Optional[float] = None
    last_low: Optional[float] = None
    last_high_bar: Optional[int] = None
    last_low_bar: Optional[int] = None
    choch_dir: int = 0
    choch_lvl: Optional[float] = None
    choch_on: bool = False
    prot_high: Optional[float] = None
    prot_low: Optional[float] = None
    hi_type: int = 0
    lo_type: int = 0
    atr_14: float = 0.0
    consolidating: bool = False
    event_note: Optional[str] = None  # e.g., "INT MSS UP", "INT BOS DOWN", "EXT MSS UP"


@dataclass
class PendingSetup:
    setup_id: str
    side: Side
    created_bar: int
    created_time: datetime
    zone_bottom: float
    zone_top: float
    sl_price: float
    struct_tp_price: Optional[float]
    entry_price: Optional[float]  # Fixed for retest_limit; None until triggered for rejection/immediate
    tp_price: Optional[float]
    tag: str  # "Wickless" | "CHOCH" | "Trend" | "External"
    entry_mode: str
    retest_bars: int
    source: str = "wickless_structure"
    touched_in_zone: bool = False
    expires_at: Optional[datetime] = None


@dataclass(frozen=True)
class TradeSignal:
    signal_id: str
    side: Side
    entry_price: float
    sl_price: float
    tp_price: float
    rr: float
    tag: str
    source: str
    created_time: datetime
    is_immediate: bool = False


@dataclass
class ExternalPriceSignal:
    signal_id: str
    symbol: str
    side: Side
    entry_price: Optional[float]  # None => market execute immediately on valid conditions
    sl_price: float
    tp_price: float
    tolerance_points: float = 15.0
    expires_at: Optional[datetime] = None
    tag: str = "ExternalBridge"
    consumed: bool = False


@dataclass
class Position:
    ticket: int
    symbol: str
    side: Side
    volume: float
    open_price: float
    sl: float
    tp: float
    profit_usd: float
    swap_usd: float = 0.0
    commission_usd: float = 0.0
    magic: int = 0
    open_time: Optional[datetime] = None
    comment: str = ""

    @property
    def net_pnl_usd(self) -> float:
        return self.profit_usd + self.swap_usd + self.commission_usd


@dataclass
class AccountInfo:
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_level: float = 0.0
    currency: str = "USD"


@dataclass
class TradeRecord:
    ticket: int
    side: Side
    volume: float
    open_time: datetime
    close_time: datetime
    open_price: float
    close_price: float
    sl: float
    tp: float
    pnl_usd: float
    r_multiple: float
    exit_reason: str  # "TP" | "SL" | "DOLLAR_TP" | "DOLLAR_SL" | "BASKET_DOLLAR_TP" | "BASKET_DOLLAR_SL" | "DAILY_LIMIT" | "EOD"
    tag: str


@dataclass
class MT5Config:
    symbol: str = "XAUUSDm"
    timeframe: str = "M1"
    magic_number: int = 771001
    tick_poll_interval_ms: int = 150
    max_spread_points: int = 45
    slippage_points: int = 20
    bars_history_count: int = 500
    auto_reconnect: bool = True
    login: Optional[int] = None
    password: str = ""
    server: str = ""
    terminal_path: str = ""


@dataclass
class StrategyConfig:
    mode: Literal["wickless_structure", "structure_momentum", "external_bridge", "hybrid"] = "wickless_structure"
    tol_ticks: float = 0.0
    min_body_ticks: float = 0.0
    entry_mode: Literal["retest_limit", "retest_rejection", "immediate"] = "retest_limit"
    pip_size: float = 0.0
    zone_pips: float = 3.0
    need_reject: bool = True
    retest_bars: int = 10
    cancel_on_flip: bool = True
    use_structure: bool = True
    internal_len: int = 3
    external_len: int = 10
    choch_zone_bars: int = 15
    require_external_alignment: bool = False
    use_consolidation: bool = True
    consol_range_atr: bool = True
    consol_bars: int = 20
    consol_mult: float = 3.5
    consol_mixed_swings: bool = True
    consol_small_body_ratio: float = 0.35
    consol_small_body_fraction: float = 0.60
    consol_alternation_fraction: float = 0.55
    block_in_consolidation: bool = False
    allow_longs: bool = True
    allow_shorts: bool = True
    sl_live: bool = True
    sl_buffer_ticks: float = 0.0
    tp_mode: Literal["fixed_rr", "structure"] = "fixed_rr"
    rr_value: float = 1.0
    min_rr: float = 1.0
    fixed_tp_distance: float = 0.0
    fixed_sl_distance: float = 0.0
    target_tp_usd: float = 0.0
    target_sl_usd: float = 0.0
    pace_mode: Literal["fast", "medium", "slow"] = "fast"
    use_sr_zones: bool = True
    sr_lookback_bars: int = 30
    sr_zone_pct: float = 0.30
    tick_confirm_enabled: bool = False
    tick_impulse_window: int = 5
    # Confirmed M1 structure/momentum mode
    htf_timeframes: list[int] = field(default_factory=lambda: [5, 15])
    htf_internal_len: int = 2
    htf_external_len: int = 5
    require_bos_or_choch: bool = True
    allow_m1_fallback: bool = False
    m1_fallback_max_lot: float = 0.01
    allow_m1_countertrend_fallback: bool = False
    allow_m1_exploration: bool = False
    pullback_bars: int = 5
    pullback_pips: float = 7.0
    rejection_required: bool = True
    momentum_min_points: float = 0.0
    momentum_min_ratio: float = 0.0
    max_spread_points: float = 0.0
    min_atr: float = 0.0
    max_atr: float = 0.0
    block_high_volatility: bool = False
    manual_news_windows: list[str] = field(default_factory=list)
    ma_period: int = 20
    ma_slope_bars: int = 3
    require_ma_slope: bool = True
    use_fvg_zones: bool = True
    use_liquidity_sweeps: bool = True
    require_fvg_or_sweep: bool = False
    sweep_lookback_bars: int = 10
    fvg_min_points: float = 0.0


@dataclass
class RiskConfig:
    sizing_mode: Literal["risk_percent", "fixed_lot"] = "risk_percent"
    risk_per_trade_pct: float = 1.0
    fixed_lot: float = 0.10
    min_lot: float = 0.01
    max_lot_per_trade: float = 3.00
    allow_scaling: bool = True
    max_open_positions: int = 3
    max_total_lots: float = 6.00
    max_total_risk_pct: float = 3.0
    scale_only_in_profit: bool = True
    scale_min_profit_r: float = 0.30
    profit_streak_scaling_enabled: bool = False
    profit_streak_step_wins: int = 2
    profit_streak_add_lots: float = 0.01
    profit_streak_max_lot: float = 0.05
    profit_streak_positions_start: int = 3
    profit_streak_positions_step_wins: int = 2
    profit_streak_positions_add: int = 1
    profit_streak_positions_max: int = 10
    move_to_be_at_r: float = 0.0
    trailing_atr_mult: float = 0.0
    max_trades_per_day: int = 0
    max_consecutive_losses: int = 0
    loss_cooldown_minutes: int = 0


@dataclass
class DollarExitConfig:
    enabled: bool = True
    trade_tp_usd: float = 15.0
    trade_sl_usd: float = 0.0
    trade_tp_usd_per_lot: float = 0.0
    trade_sl_usd_per_lot: float = 0.0
    stagnation_enabled: bool = False
    stagnation_min_minutes: int = 15
    stagnation_pnl_band_usd: float = 0.25
    stagnation_flip_count: int = 6
    basket_tp_usd: float = 15.0
    basket_sl_usd: float = 30.0
    daily_max_profit_usd: float = 150.0
    daily_max_loss_usd: float = 75.0


@dataclass
class SessionConfig:
    enabled: bool = False
    allowed_utc_hours: list[tuple[int, int]] = field(default_factory=lambda: [(7, 21)])


@dataclass
class ExternalBridgeConfig:
    signal_file: str = "signals/external_signals.json"
    webhook_enabled: bool = False
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8787
    webhook_secret: str = ""
    default_expiry_minutes: int = 15


@dataclass
class TelegramConfig:
    enabled: bool = True
    bot_token: str = ""
    admin_chat_id: Optional[int] = None
    notify_structure_shifts: bool = True
    notify_setups: bool = True
    notify_fills_and_closes: bool = True


@dataclass
class LoggingConfig:
    market_intel_enabled: bool = True
    market_intel_path: str = "reports/market_intel.jsonl"


@dataclass
class BotConfig:
    mt5: MT5Config = field(default_factory=MT5Config)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    dollar_exits: DollarExitConfig = field(default_factory=DollarExitConfig)
    sessions: SessionConfig = field(default_factory=SessionConfig)
    external_bridge: ExternalBridgeConfig = field(default_factory=ExternalBridgeConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


@dataclass
class BacktestMetrics:
    total_trades: int
    wins: int
    losses: int
    win_rate_pct: float
    net_profit_usd: float
    return_pct: float
    profit_factor: float
    expectancy_usd: float
    max_drawdown_usd: float
    max_drawdown_pct: float
    sharpe_ratio: float
    avg_r_multiple: float
    total_r: float
    dollar_tp_closes: int
    dollar_sl_closes: int
    cancelled_setups: int
    exit_breakdown: dict[str, int] = field(default_factory=dict)
