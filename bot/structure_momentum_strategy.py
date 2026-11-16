from __future__ import annotations

import logging
import uuid
from collections import deque
from dataclasses import replace
from datetime import datetime
from typing import Optional

from bot.models import Candle, PendingSetup, SessionConfig, Side, StrategyConfig, StructureSnapshot, Tick, TradeSignal
from bot.structure_engine import StructureTracker

logger = logging.getLogger(__name__)


class StructureMomentumStrategy:
    """Confirmed-close M1 BOS/CHOCH pullback strategy with M5/M15 alignment.

    Higher-timeframe bars are built only from completed M1 candles.  No tick or
    intrabar high/low is allowed to confirm a break; the close must cross the
    protected level.
    """

    def __init__(
        self,
        cfg: StrategyConfig,
        symbol: str,
        mintick: float,
        sessions: Optional[SessionConfig] = None,
        max_spread_points: float = 0.0,
    ) -> None:
        self.cfg = cfg
        self.symbol = symbol
        self.mintick = mintick
        self.sessions = sessions or SessionConfig()
        self.max_spread_points = max_spread_points or cfg.max_spread_points
        self.m1 = StructureTracker(cfg)
        self.htf: dict[int, StructureTracker] = {}
        self.htf_snapshots: dict[int, StructureSnapshot] = {}
        for minutes in cfg.htf_timeframes:
            htf_cfg = replace(cfg, internal_len=cfg.htf_internal_len, external_len=cfg.htf_external_len)
            self.htf[minutes] = StructureTracker(htf_cfg)
        self._htf_builders: dict[int, list[Candle]] = {minutes: [] for minutes in cfg.htf_timeframes}
        self.pending_setups: list[PendingSetup] = []
        self.tick_buffer: deque[Tick] = deque(maxlen=max(20, cfg.tick_impulse_window * 4))
        self.last_snapshot: Optional[StructureSnapshot] = None
        self.cancelled_setups_count = 0
        self.skip_reasons: list[str] = []
        self._last_sr: dict = {}

    @staticmethod
    def is_confirmed_break(close: float, level: float, side: Side) -> bool:
        """A BOS is confirmed by the closed candle, never by its wick."""
        return close > level if side == Side.BUY else close < level

    def _record_skip(self, reason: str) -> None:
        self.skip_reasons.append(reason)
        logger.info("Structure momentum setup skipped: %s", reason)

    def _session_ok(self, now: datetime) -> bool:
        if not self.sessions.enabled:
            return True
        hour = now.hour
        return any(
            (start <= hour < end) if start <= end else (hour >= start or hour < end)
            for start, end in self.sessions.allowed_utc_hours
        )

    def _news_ok(self, now: datetime) -> bool:
        for raw in self.cfg.manual_news_windows:
            try:
                start_raw, end_raw = [part.strip() for part in raw.split("/", 1)]
                start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                if start <= now <= end:
                    return False
            except (ValueError, TypeError):
                logger.warning("Ignoring invalid manual news window: %s", raw)
        return True

    def _filters_ok(self, candle: Candle, snapshot: StructureSnapshot) -> bool:
        if not self._session_ok(candle.time):
            self._record_skip("outside_session")
            return False
        spread = float(candle.spread)
        if self.max_spread_points > 0 and spread > self.max_spread_points:
            self._record_skip(f"spread_too_wide:{spread:.1f}")
            return False
        if not self._news_ok(candle.time):
            self._record_skip("manual_news_window")
            return False
        if snapshot.consolidating and self.cfg.block_in_consolidation:
            self._record_skip("consolidation")
            return False
        if self.cfg.min_atr > 0 and snapshot.atr_14 < self.cfg.min_atr:
            self._record_skip("volatility_below_minimum")
            return False
        if self.cfg.max_atr > 0 and snapshot.atr_14 > self.cfg.max_atr:
            self._record_skip("volatility_above_maximum")
            return False
        return True

    def _update_htf(self, candle: Candle) -> None:
        for minutes, builder in self._htf_builders.items():
            bucket = (candle.time.hour * 60 + candle.time.minute) // minutes
            if builder:
                previous_bucket = (builder[0].time.hour * 60 + builder[0].time.minute) // minutes
                if bucket != previous_bucket or candle.time.date() != builder[0].time.date():
                    aggregate = self._aggregate(builder)
                    self.htf_snapshots[minutes] = self.htf[minutes].update(aggregate)
                    builder.clear()
            builder.append(candle)

    @staticmethod
    def _aggregate(candles: list[Candle]) -> Candle:
        first, last = candles[0], candles[-1]
        return Candle(
            time=first.time,
            open=first.open,
            high=max(c.high for c in candles),
            low=min(c.low for c in candles),
            close=last.close,
            tick_volume=sum(c.tick_volume for c in candles),
            spread=max(c.spread for c in candles),
        )

    def _aligned_side(self) -> Optional[Side]:
        trends = [snap.external_trend or snap.internal_trend for snap in self.htf_snapshots.values()]
        if len(trends) < len(self.htf) or not trends or any(trend == 0 for trend in trends):
            return None
        if all(trend == 1 for trend in trends):
            return Side.BUY
        if all(trend == -1 for trend in trends):
            return Side.SELL
        return None

    def _ma_slope_ok(self, side: Side) -> bool:
        if not self.cfg.require_ma_slope:
            return True
        candles = self.m1.candles
        period = max(2, self.cfg.ma_period)
        slope_bars = max(1, self.cfg.ma_slope_bars)
        if len(candles) < period + slope_bars:
            return False
        current = sum(c.close for c in candles[-period:]) / period
        previous_slice = candles[-period - slope_bars : -slope_bars]
        previous = sum(c.close for c in previous_slice) / period
        return current > previous if side == Side.BUY else current < previous

    def _bullish_fvg(self) -> Optional[tuple[float, float]]:
        if not self.cfg.use_fvg_zones or len(self.m1.candles) < 3:
            return None
        left, _, right = self.m1.candles[-3:]
        gap = right.low - left.high
        if gap >= max(0.0, self.cfg.fvg_min_points * self.mintick):
            return left.high, right.low
        return None

    def _bearish_fvg(self) -> Optional[tuple[float, float]]:
        if not self.cfg.use_fvg_zones or len(self.m1.candles) < 3:
            return None
        left, _, right = self.m1.candles[-3:]
        gap = left.low - right.high
        if gap >= max(0.0, self.cfg.fvg_min_points * self.mintick):
            return right.high, left.low
        return None

    def _liquidity_sweep(self, side: Side) -> Optional[float]:
        if not self.cfg.use_liquidity_sweeps:
            return None
        lookback = max(2, self.cfg.sweep_lookback_bars)
        if len(self.m1.candles) <= lookback:
            return None
        current = self.m1.candles[-1]
        prior = self.m1.candles[-lookback - 1 : -1]
        if side == Side.BUY:
            level = min(c.low for c in prior)
            return current.low if current.low < level and current.close > level else None
        level = max(c.high for c in prior)
        return current.high if current.high > level and current.close < level else None

    def _resolve_tp(self, side: Side, entry: float, sl: float) -> Optional[tuple[float, float]]:
        if self.cfg.fixed_tp_distance > 0:
            tp = entry + self.cfg.fixed_tp_distance if side == Side.BUY else entry - self.cfg.fixed_tp_distance
            risk = abs(entry - sl)
            rr = (self.cfg.fixed_tp_distance / risk) if risk > 0 else 0.0
            return tp, rr
        risk = entry - sl if side == Side.BUY else sl - entry
        if risk <= 0:
            return None
        tp = entry + risk * self.cfg.rr_value if side == Side.BUY else entry - risk * self.cfg.rr_value
        return tp, abs(tp - entry) / risk

    def _evaluate_sr_zone(self, candle: Candle) -> tuple[Optional[float], Optional[float], float, str, bool, float]:
        """
        Calculates Support/Resistance zone from recent M1 candles and detects
        consolidation breakouts (like tight range -> sharp impulse move).

        Returns: (sr_low, sr_high, pos_ratio, zone_state, consol_detected, consol_range)

        zone_state values:
          "AT_SUPPORT"        -- price near bottom of range; prefer BUY
          "AT_RESISTANCE"     -- price near top of range; prefer SELL
          "BREAKOUT_UP"       -- close above prior range high; aggressive BUY
          "BREAKDOWN_DOWN"    -- close below prior range low; aggressive SELL
          "CONSOL_BREAK_UP"   -- broke above a tight consolidation zone (high-confidence BUY)
          "CONSOL_BREAK_DOWN" -- broke below a tight consolidation zone (high-confidence SELL)
          "IN_BETWEEN"        -- mid-range; follow momentum trend
        """
        candles = self.m1.candles
        lookback = max(5, self.cfg.sr_lookback_bars)
        if len(candles) < 5:
            return None, None, 0.5, "IN_BETWEEN", False, 0.0

        window = candles[-lookback:]
        # Exclude the just-closed candle so we evaluate the PRIOR range
        eval_window = window[:-1] if len(window) > 1 else window

        sr_high = max(c.high for c in eval_window)
        sr_low = min(c.low for c in eval_window)
        cur = candle.close
        rng = sr_high - sr_low

        # -- Consolidation detection -----------------------------------------
        # Look at the most recent consol_bars candles in eval_window to see if
        # price was coiling in a tighter-than-average range before this candle.
        consol_detected = False
        consol_range = 0.0
        consol_bars = min(10, max(5, len(eval_window) // 3))
        recent_slice = eval_window[-consol_bars:] if len(eval_window) >= consol_bars else eval_window
        if len(recent_slice) >= 3:
            consol_high = max(c.high for c in recent_slice)
            consol_low = min(c.low for c in recent_slice)
            consol_range = consol_high - consol_low
            # Tight consolidation: recent range < 40% of full lookback range AND
            # the current candle closes OUTSIDE that tight range
            tight_threshold = max(0.001, rng * 0.40) if rng > 0 else 0.001
            if consol_range > 0 and consol_range < tight_threshold:
                consol_detected = True
                # Did we just break OUT of that consolidation on this candle?
                if candle.close > consol_high and candle.close > candle.open:
                    return sr_low, sr_high, 1.0, "CONSOL_BREAK_UP", True, consol_range
                if candle.close < consol_low and candle.close < candle.open:
                    return sr_low, sr_high, 0.0, "CONSOL_BREAK_DOWN", True, consol_range

        # -- Standard breakout check -----------------------------------------
        if cur > sr_high:
            return sr_low, sr_high, 1.0, "BREAKOUT_UP", consol_detected, consol_range
        if cur < sr_low:
            return sr_low, sr_high, 0.0, "BREAKDOWN_DOWN", consol_detected, consol_range

        if rng <= 0:
            return sr_low, sr_high, 0.5, "IN_BETWEEN", consol_detected, consol_range

        pos = (cur - sr_low) / rng
        threshold = max(0.10, min(0.45, self.cfg.sr_zone_pct))
        if pos <= threshold:
            return sr_low, sr_high, pos, "AT_SUPPORT", consol_detected, consol_range
        if pos >= (1.0 - threshold):
            return sr_low, sr_high, pos, "AT_RESISTANCE", consol_detected, consol_range
        return sr_low, sr_high, pos, "IN_BETWEEN", consol_detected, consol_range


    def on_bar_close(self, candle: Candle, emit_signals: bool = True) -> tuple[StructureSnapshot, list[TradeSignal]]:
        self._update_htf(candle)
        snapshot = self.m1.update(candle)
        self.last_snapshot = snapshot
        signals: list[TradeSignal] = []
        if not emit_signals:
            return snapshot, signals

        # Support & Resistance Zone Analysis
        sr_side: Optional[Side] = None
        sr_tag = "M1_MOMENTUM"
        if self.cfg.use_sr_zones:
            sr_low, sr_high, sr_pos, sr_zone, sr_consol, sr_consol_range = self._evaluate_sr_zone(candle)
            # Store SR context so the engine can pass it to the learning logger
            self._last_sr = {
                "sr_low": sr_low, "sr_high": sr_high,
                "pos_ratio": sr_pos, "zone_state": sr_zone,
                "consol_detected": sr_consol, "consol_range_points": sr_consol_range,
            }
            if sr_zone in ("CONSOL_BREAK_UP", "BREAKOUT_UP"):
                # High-confidence BUY — price broke above prior range/consolidation
                sr_side = Side.BUY
                sr_tag = sr_zone  # "CONSOL_BREAK_UP" or "BREAKOUT_UP"
            elif sr_zone in ("CONSOL_BREAK_DOWN", "BREAKDOWN_DOWN"):
                # High-confidence SELL — price broke below prior range/consolidation
                sr_side = Side.SELL
                sr_tag = sr_zone  # "CONSOL_BREAK_DOWN" or "BREAKDOWN_DOWN"
            elif sr_zone == "AT_SUPPORT":
                if candle.close > candle.open:
                    sr_side = Side.BUY
                    sr_tag = "SR_SUPPORT_BOUNCE"
                else:
                    self._record_skip("at_support_block_sell")
            elif sr_zone == "AT_RESISTANCE":
                if candle.close < candle.open:
                    sr_side = Side.SELL
                    sr_tag = "SR_RESISTANCE_REJECT"
                else:
                    self._record_skip("at_resistance_block_buy")
            elif sr_zone == "IN_BETWEEN":
                if snapshot.internal_trend > 0 and candle.close > candle.open:
                    sr_side = Side.BUY
                    sr_tag = "SR_FLOW_UP"
                elif snapshot.internal_trend < 0 and candle.close < candle.open:
                    sr_side = Side.SELL
                    sr_tag = "SR_FLOW_DOWN"
                elif candle.close > candle.open and self.cfg.allow_m1_exploration:
                    sr_side = Side.BUY
                    sr_tag = "M1_MOMENTUM_UP"
                elif candle.close < candle.open and self.cfg.allow_m1_exploration:
                    sr_side = Side.SELL
                    sr_tag = "M1_MOMENTUM_DOWN"

        # A structure event is generated only after StructureTracker sees a
        # completed close through the confirmed swing level.
        event = snapshot.event_note or ""
        bos_side = Side.BUY if "BOS UP" in event or "MSS UP" in event else Side.SELL if "BOS DOWN" in event or "MSS DOWN" in event else None
        aligned = self._aligned_side()

        chosen_side = sr_side if self.cfg.use_sr_zones and sr_side is not None else bos_side
        chosen_tag = sr_tag if self.cfg.use_sr_zones and sr_side is not None else "BOS"

        if chosen_side is None and self.cfg.allow_m1_exploration and snapshot.internal_trend:
            exploratory_side = Side.BUY if snapshot.internal_trend > 0 else Side.SELL
            candle_direction_ok = (
                candle.close > candle.open if exploratory_side == Side.BUY else candle.close < candle.open
            )
            if candle_direction_ok:
                chosen_side = exploratory_side
                chosen_tag = "M1_EXPLORATION"

        if chosen_side is not None:
            has_pending_side = any(
                setup.side == chosen_side for setup in self.pending_setups
            )
            side_allowed = (chosen_side == Side.BUY and self.cfg.allow_longs) or (
                chosen_side == Side.SELL and self.cfg.allow_shorts
            )
            if not has_pending_side and side_allowed and self._filters_ok(candle, snapshot) and self._ma_slope_ok(chosen_side):
                zone = self.cfg.pullback_pips * (
                    self.cfg.pip_size
                    or (0.1 if "XAU" in self.symbol.upper() else self.mintick * 10)
                )
                if chosen_side == Side.BUY:
                    zone_bottom, zone_top = candle.close - zone, candle.close
                    entry = candle.close if self.cfg.entry_mode == "immediate" else zone_top
                    sl = (entry - self.cfg.fixed_sl_distance) if self.cfg.fixed_sl_distance > 0 else min(snapshot.prot_low or candle.low, candle.low)
                else:
                    zone_bottom, zone_top = candle.close, candle.close + zone
                    entry = candle.close if self.cfg.entry_mode == "immediate" else zone_bottom
                    sl = (entry + self.cfg.fixed_sl_distance) if self.cfg.fixed_sl_distance > 0 else max(snapshot.prot_high or candle.high, candle.high)
                resolved = self._resolve_tp(chosen_side, entry, sl)
                if resolved:
                    tp, _ = resolved
                    self.pending_setups.append(
                        PendingSetup(
                            uuid.uuid4().hex[:10],
                            chosen_side,
                            snapshot.bar_index,
                            candle.time,
                            zone_bottom,
                            zone_top,
                            sl,
                            None,
                            entry if self.cfg.entry_mode == "retest_limit" else None,
                            tp if self.cfg.entry_mode == "retest_limit" else None,
                            chosen_tag,
                            self.cfg.entry_mode,
                            self.cfg.pullback_bars,
                            source="structure_momentum",
                        )
                    )
        m1_fallback = bos_side is not None and aligned is None and self.cfg.allow_m1_fallback
        countertrend_fallback = (
            bos_side is not None
            and aligned is not None
            and aligned != bos_side
            and self.cfg.allow_m1_countertrend_fallback
        )
        if bos_side is not None and aligned != bos_side and not (m1_fallback or countertrend_fallback):
            self._record_skip("htf_not_aligned")
        elif bos_side is not None and self._filters_ok(candle, snapshot):
            if not self._ma_slope_ok(bos_side):
                self._record_skip("ma_slope_disagrees")
                return snapshot, signals
            fvg = self._bullish_fvg() if bos_side == Side.BUY else self._bearish_fvg()
            sweep = self._liquidity_sweep(bos_side)
            if countertrend_fallback and fvg is None and sweep is None:
                self._record_skip("countertrend_no_zone")
                return snapshot, signals
            if self.cfg.require_fvg_or_sweep and fvg is None and sweep is None:
                self._record_skip("no_fvg_or_liquidity_sweep")
                return snapshot, signals
            if bos_side == Side.BUY and self.cfg.allow_longs:
                zone = self.cfg.pullback_pips * (self.cfg.pip_size or (0.1 if "XAU" in self.symbol.upper() else self.mintick * 10))
                if fvg is not None:
                    zone_bottom, zone_top = fvg
                else:
                    zone_bottom, zone_top = candle.close - zone, candle.close
                entry = candle.close if self.cfg.entry_mode == "immediate" else zone_top
                sl = (entry - self.cfg.fixed_sl_distance) if self.cfg.fixed_sl_distance > 0 else min(snapshot.prot_low or candle.low, sweep or candle.low)
                resolved = self._resolve_tp(bos_side, entry, sl)
                if resolved:
                    tp, _ = resolved
                    self.pending_setups.append(PendingSetup(uuid.uuid4().hex[:10], bos_side, snapshot.bar_index, candle.time, zone_bottom, zone_top, sl, None, None if self.cfg.entry_mode != "retest_limit" else entry, tp if self.cfg.entry_mode == "retest_limit" else None, "M1_COUNTERTREND" if countertrend_fallback else "M1_FALLBACK" if m1_fallback else "BOS/FVG" if fvg else "BOS/SWEEP", self.cfg.entry_mode, self.cfg.pullback_bars, source="structure_momentum"))
            elif bos_side == Side.SELL and self.cfg.allow_shorts:
                zone = self.cfg.pullback_pips * (self.cfg.pip_size or (0.1 if "XAU" in self.symbol.upper() else self.mintick * 10))
                if fvg is not None:
                    zone_bottom, zone_top = fvg
                else:
                    zone_bottom, zone_top = candle.close, candle.close + zone
                entry = candle.close if self.cfg.entry_mode == "immediate" else zone_bottom
                sl = (entry + self.cfg.fixed_sl_distance) if self.cfg.fixed_sl_distance > 0 else max(snapshot.prot_high or candle.high, sweep or candle.high)
                resolved = self._resolve_tp(bos_side, entry, sl)
                if resolved:
                    tp, _ = resolved
                    self.pending_setups.append(PendingSetup(uuid.uuid4().hex[:10], bos_side, snapshot.bar_index, candle.time, zone_bottom, zone_top, sl, None, entry if self.cfg.entry_mode == "retest_limit" else None, tp if self.cfg.entry_mode == "retest_limit" else None, "M1_COUNTERTREND" if countertrend_fallback else "M1_FALLBACK" if m1_fallback else "BOS/FVG" if fvg else "BOS/SWEEP", self.cfg.entry_mode, self.cfg.pullback_bars, source="structure_momentum"))

        remaining: list[PendingSetup] = []
        for setup in self.pending_setups:
            age = snapshot.bar_index - setup.created_bar
            if setup.entry_mode == "immediate" and age == 0:
                entry = candle.close
                sl = (entry - self.cfg.fixed_sl_distance) if (self.cfg.fixed_sl_distance > 0 and setup.side == Side.BUY) else (entry + self.cfg.fixed_sl_distance) if (self.cfg.fixed_sl_distance > 0 and setup.side == Side.SELL) else setup.sl_price
                resolved = self._resolve_tp(setup.side, entry, sl)
                if resolved:
                    tp, rr = resolved
                    signals.append(TradeSignal(setup.setup_id, setup.side, entry, sl, tp, rr, setup.tag, setup.source, candle.time, True))
                continue
            if age < 1:
                remaining.append(setup)
                continue
            touched = candle.low <= setup.zone_top if setup.side == Side.BUY else candle.high >= setup.zone_bottom
            rejected = candle.close > candle.open if setup.side == Side.BUY else candle.close < candle.open
            if touched and (not self.cfg.rejection_required or rejected):
                if not self._tick_momentum_confirmed(setup.side):
                    self._record_skip("tick_momentum")
                    remaining.append(setup)
                    continue
                entry = setup.entry_price if setup.entry_mode == "retest_limit" and setup.entry_price is not None else candle.close
                sl = (entry - self.cfg.fixed_sl_distance) if (self.cfg.fixed_sl_distance > 0 and setup.side == Side.BUY) else (entry + self.cfg.fixed_sl_distance) if (self.cfg.fixed_sl_distance > 0 and setup.side == Side.SELL) else setup.sl_price
                resolved = self._resolve_tp(setup.side, entry, sl)
                if resolved:
                    tp, rr = resolved
                    signals.append(TradeSignal(setup.setup_id, setup.side, entry, sl, tp, rr, setup.tag, setup.source, candle.time))
                continue
            if age >= setup.retest_bars:
                self.cancelled_setups_count += 1
                self._record_skip("pullback_expired")
            else:
                remaining.append(setup)
        self.pending_setups = remaining
        return snapshot, signals

    def _tick_momentum_confirmed(self, side: Side) -> bool:
        if not self.cfg.tick_confirm_enabled:
            return True
        if len(self.tick_buffer) < self.cfg.tick_impulse_window:
            return False
        recent = list(self.tick_buffer)[-self.cfg.tick_impulse_window:]
        delta = recent[-1].mid - recent[0].mid
        points = abs(delta) / self.mintick if self.mintick > 0 else 0.0
        ratio = sum(1 for a, b in zip(recent, recent[1:]) if (b.mid - a.mid) * side.sign > 0) / max(1, len(recent) - 1)
        return delta * side.sign >= self.cfg.momentum_min_points * self.mintick and ratio >= self.cfg.momentum_min_ratio

    def on_tick(self, tick: Tick) -> list[TradeSignal]:
        self.tick_buffer.append(tick)
        fired: list[TradeSignal] = []
        remaining: list[PendingSetup] = []
        for setup in self.pending_setups:
            if setup.entry_mode != "retest_limit" or setup.entry_price is None or setup.tp_price is None:
                remaining.append(setup)
                continue
            touched = tick.ask <= setup.entry_price if setup.side == Side.BUY else tick.bid >= setup.entry_price
            if touched and self._tick_momentum_confirmed(setup.side):
                entry = tick.ask if setup.side == Side.BUY else tick.bid
                sl = (entry - self.cfg.fixed_sl_distance) if (self.cfg.fixed_sl_distance > 0 and setup.side == Side.BUY) else (entry + self.cfg.fixed_sl_distance) if (self.cfg.fixed_sl_distance > 0 and setup.side == Side.SELL) else setup.sl_price
                resolved = self._resolve_tp(setup.side, entry, sl)
                if resolved:
                    tp, rr = resolved
                    fired.append(TradeSignal(setup.setup_id, setup.side, entry, sl, tp, rr, setup.tag, setup.source, tick.time))
                    continue
            elif touched:
                self._record_skip("tick_momentum")
            remaining.append(setup)
        self.pending_setups = remaining
        return fired
