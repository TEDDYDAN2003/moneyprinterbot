"""
Trade Learning Logger
=====================
Records every trade open and close with full market context (SR zone, candle
shape, momentum, spread, ATR, structural state, etc.) into a JSONL file.

The growing log forms a training dataset that can be analysed later to:
  - Identify which signal tags / zone states produce the best win rate.
  - Tune sr_lookback_bars, sr_zone_pct, and pace_mode per market condition.
  - Spot high-probability consolidation-breakout patterns on M1.
  - Provide evidence-based config adjustments without manual back-testing.

Each record is a self-contained JSON object with these top-level sections:
  event       : "OPEN" | "CLOSE"
  timestamp   : ISO-8601 UTC
  ticket      : MT5 position ticket
  symbol      : traded symbol
  side        : "BUY" | "SELL"
  lots        : position volume
  entry       : entry price
  sl / tp     : price levels at order placement
  pnl_usd     : realised USD PnL (CLOSE events only)
  outcome     : "WIN" | "LOSS" | "BREAKEVEN" (CLOSE events only)
  tag         : signal tag e.g. "SR_BREAKOUT_UP", "SR_SUPPORT_BOUNCE"
  pace_mode   : "fast" | "medium" | "slow"
  market      : candle OHLC + spread + ATR snapshot at entry
  sr_zone     : support/resistance levels + zone_state + pos_ratio
  structure   : internal/external trend + event_note + consolidating flag
  config_snap : key config params at the time of the trade
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

LEARNING_LOG_PATH = "reports/trade_learning.jsonl"


@dataclass
class SRSnapshot:
    sr_low: Optional[float]
    sr_high: Optional[float]
    pos_ratio: float
    zone_state: str  # AT_SUPPORT | AT_RESISTANCE | BREAKOUT_UP | BREAKDOWN_DOWN | IN_BETWEEN
    consol_detected: bool = False
    consol_range_points: float = 0.0  # Width of the consolidation range in price points


@dataclass
class MarketSnapshot:
    open: float
    high: float
    low: float
    close: float
    spread_points: float
    atr_14: float
    candle_body_pct: float   # body / full_range 0-1; 0 = doji, 1 = marubozu
    candle_direction: str    # "BULL" | "BEAR" | "DOJI"


@dataclass
class StructureSnapshot:
    internal_trend: int   # 1=bullish, -1=bearish, 0=neutral
    external_trend: int
    event_note: str
    consolidating: bool


@dataclass
class TradeLearnRecord:
    event: str              # "OPEN" | "CLOSE"
    timestamp: str
    ticket: int
    symbol: str
    side: str
    lots: float
    entry: float
    sl: float
    tp: float
    tag: str
    pace_mode: str
    market: MarketSnapshot
    sr_zone: SRSnapshot
    structure: StructureSnapshot
    # CLOSE-only fields (default None / empty):
    pnl_usd: Optional[float] = None
    outcome: Optional[str] = None      # "WIN" | "LOSS" | "BREAKEVEN"
    bars_held: Optional[int] = None    # Number of M1 bars the position was open
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None  # "DOLLAR_TP" | "DOLLAR_SL" | "BROKER_TP" | "BROKER_SL" | "MANUAL"
    config_snap: Optional[dict[str, Any]] = None


class TradeLearningLogger:
    """
    Logs OPEN and CLOSE events with full market context.

    Usage (from engine.py):
        self.learning = TradeLearningLogger(enabled=True, path="reports/trade_learning.jsonl")

        # On trade open — after open_market_order succeeds:
        self.learning.log_open(ticket, sig, lots, candle, snap, sr_snap, pace_mode, cfg)

        # On position close detected (in _manage_open_positions):
        self.learning.log_close(ticket, pnl_usd, candle, snap, sr_snap, reason, cfg)
    """

    def __init__(self, enabled: bool, path: str = LEARNING_LOG_PATH) -> None:
        self.enabled = enabled
        self.path = Path(path)
        # Store open trade context keyed by ticket for later CLOSE enrichment
        self._open_ctx: dict[int, TradeLearnRecord] = {}

    def _write(self, record: TradeLearnRecord) -> None:
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                # Convert nested dataclasses to plain dicts
                raw = asdict(record)
                fh.write(json.dumps(raw, separators=(",", ":"), default=str) + "\n")
        except OSError:
            logger.exception("Unable to write trade-learning record to %s", self.path)

    @staticmethod
    def _candle_snapshot(candle: Any, spread_points: float, atr_14: float) -> MarketSnapshot:
        full_range = candle.high - candle.low
        body = abs(candle.close - candle.open)
        body_pct = (body / full_range) if full_range > 0 else 0.0
        if body_pct < 0.1:
            direction = "DOJI"
        elif candle.close >= candle.open:
            direction = "BULL"
        else:
            direction = "BEAR"
        return MarketSnapshot(
            open=round(candle.open, 5),
            high=round(candle.high, 5),
            low=round(candle.low, 5),
            close=round(candle.close, 5),
            spread_points=round(spread_points, 1),
            atr_14=round(atr_14, 5),
            candle_body_pct=round(body_pct, 3),
            candle_direction=direction,
        )

    @staticmethod
    def _config_snap(cfg: Any) -> dict[str, Any]:
        """Capture the key config knobs that affect trade quality."""
        try:
            s = cfg.strategy
            r = cfg.risk
            d = cfg.dollar_exits
            return {
                "pace_mode": getattr(s, "pace_mode", "fast"),
                "use_sr_zones": getattr(s, "use_sr_zones", True),
                "sr_lookback_bars": getattr(s, "sr_lookback_bars", 30),
                "sr_zone_pct": getattr(s, "sr_zone_pct", 0.30),
                "target_tp_usd": getattr(s, "target_tp_usd", 1.0),
                "target_sl_usd": getattr(s, "target_sl_usd", 6.0),
                "fixed_lot": r.fixed_lot,
                "max_open_positions": r.max_open_positions,
                "entry_mode": s.entry_mode,
                "trade_tp_usd": d.trade_tp_usd,
                "trade_sl_usd": d.trade_sl_usd,
            }
        except Exception:
            return {}

    def log_open(
        self,
        ticket: int,
        signal: Any,          # TradeSignal
        lots: float,
        candle: Any,          # Candle – most recent closed M1 bar
        structure_snap: Any,  # StructureSnapshot from strategy
        sr_snap: SRSnapshot,
        spread_points: float,
        pace_mode: str,
        cfg: Any,             # BotConfig
    ) -> None:
        if not self.enabled:
            return
        atr = structure_snap.atr_14 if structure_snap else 0.0
        mkt = self._candle_snapshot(candle, spread_points, atr)
        struct = StructureSnapshot(
            internal_trend=structure_snap.internal_trend if structure_snap else 0,
            external_trend=structure_snap.external_trend if structure_snap else 0,
            event_note=structure_snap.event_note or "" if structure_snap else "",
            consolidating=structure_snap.consolidating if structure_snap else False,
        )
        rec = TradeLearnRecord(
            event="OPEN",
            timestamp=datetime.now(timezone.utc).isoformat(),
            ticket=ticket,
            symbol=getattr(cfg.mt5, "symbol", "UNKNOWN"),
            side=signal.side.value,
            lots=lots,
            entry=round(signal.entry_price, 5),
            sl=round(signal.sl_price, 5),
            tp=round(signal.tp_price, 5),
            tag=signal.tag,
            pace_mode=pace_mode,
            market=mkt,
            sr_zone=sr_snap,
            structure=struct,
            config_snap=self._config_snap(cfg),
        )
        self._open_ctx[ticket] = rec
        self._write(rec)
        logger.debug("TradeLearning OPEN ticket=%d tag=%s", ticket, signal.tag)

    def log_close(
        self,
        ticket: int,
        pnl_usd: float,
        exit_price: float,
        exit_reason: str,
        candle: Any = None,           # Optional – latest candle at close time
        structure_snap: Any = None,
        sr_snap: Optional[SRSnapshot] = None,
        cfg: Any = None,
    ) -> None:
        if not self.enabled:
            return
        # Build close record — start from the matching open record if available
        open_rec = self._open_ctx.pop(ticket, None)
        if open_rec is None:
            # No OPEN record on file (e.g. position opened before bot started) — emit partial record
            rec = TradeLearnRecord(
                event="CLOSE",
                timestamp=datetime.now(timezone.utc).isoformat(),
                ticket=ticket,
                symbol=getattr(cfg.mt5, "symbol", "UNKNOWN") if cfg else "UNKNOWN",
                side="UNKNOWN",
                lots=0.0,
                entry=0.0,
                sl=0.0,
                tp=0.0,
                tag="UNKNOWN",
                pace_mode="unknown",
                market=MarketSnapshot(0, 0, 0, 0, 0, 0, 0, "DOJI"),
                sr_zone=sr_snap or SRSnapshot(None, None, 0.5, "IN_BETWEEN"),
                structure=StructureSnapshot(0, 0, "", False),
                pnl_usd=round(pnl_usd, 2),
                outcome=_pnl_outcome(pnl_usd),
                exit_price=round(exit_price, 5),
                exit_reason=exit_reason,
            )
        else:
            # Build CLOSE event with updated data
            rec = TradeLearnRecord(
                event="CLOSE",
                timestamp=datetime.now(timezone.utc).isoformat(),
                ticket=ticket,
                symbol=open_rec.symbol,
                side=open_rec.side,
                lots=open_rec.lots,
                entry=open_rec.entry,
                sl=open_rec.sl,
                tp=open_rec.tp,
                tag=open_rec.tag,
                pace_mode=open_rec.pace_mode,
                market=open_rec.market,  # Keep market conditions at OPEN time
                sr_zone=open_rec.sr_zone,
                structure=open_rec.structure,
                pnl_usd=round(pnl_usd, 2),
                outcome=_pnl_outcome(pnl_usd),
                exit_price=round(exit_price, 5),
                exit_reason=exit_reason,
                config_snap=open_rec.config_snap,
            )
        self._write(rec)
        logger.info(
            "TradeLearning CLOSE ticket=%d pnl=%.2f outcome=%s reason=%s",
            ticket, pnl_usd, rec.outcome, exit_reason,
        )


def _pnl_outcome(pnl: float) -> str:
    if pnl > 0.01:
        return "WIN"
    if pnl < -0.01:
        return "LOSS"
    return "BREAKEVEN"
