from __future__ import annotations

import uuid
from collections import deque
from typing import Optional

from bot.models import Candle, PendingSetup, Side, StrategyConfig, StructureSnapshot, Tick, TradeSignal
from bot.structure_engine import StructureTracker


class WicklessStructureStrategy:
    """
    Faithful Python implementation of the Pine Script v6 'Wickless Structure Strategy',
    extended with live tick-by-tick retest precision and optional external alignment.
    """

    def __init__(self, cfg: StrategyConfig, symbol: str, mintick: float) -> None:
        self.cfg = cfg
        self.symbol = symbol
        self.mintick = mintick
        self.structure = StructureTracker(cfg)
        self.pending_setups: list[PendingSetup] = []
        self.tick_buffer: deque[Tick] = deque(maxlen=max(20, cfg.tick_impulse_window * 4))
        self.last_snapshot: Optional[StructureSnapshot] = None
        self.bull_wickless_count: int = 0
        self.bear_wickless_count: int = 0
        self.cancelled_setups_count: int = 0

    def _pip_size(self) -> float:
        if self.cfg.pip_size > 0:
            return self.cfg.pip_size
        if "XAU" in self.symbol.upper():
            return 0.1
        return self.mintick * 10.0

    def _resolve_tp(
        self,
        side: Side,
        entry: float,
        sl: float,
        struct_tp: Optional[float],
    ) -> Optional[tuple[float, float]]:
        risk = (entry - sl) if side == Side.BUY else (sl - entry)
        if risk <= 0:
            return None

        if self.cfg.tp_mode == "fixed_rr":
            tp = entry + risk * self.cfg.rr_value if side == Side.BUY else entry - risk * self.cfg.rr_value
        else:
            st_ok = struct_tp is not None and (
                (side == Side.BUY and struct_tp > entry) or (side == Side.SELL and struct_tp < entry)
            )
            if st_ok and struct_tp is not None:
                tp = struct_tp
            else:
                tp = entry + risk * self.cfg.rr_value if side == Side.BUY else entry - risk * self.cfg.rr_value

        rr = abs(tp - entry) / risk
        if self.cfg.tp_mode == "structure" and rr < self.cfg.min_rr:
            return None
        return tp, rr

    def _tick_impulse_confirmed(self, side: Side) -> bool:
        if not self.cfg.tick_confirm_enabled:
            return True
        win = self.cfg.tick_impulse_window
        if len(self.tick_buffer) < win:
            return True
        recent = list(self.tick_buffer)[-win:]
        mids = [t.mid for t in recent]
        delta = mids[-1] - mids[0]
        if side == Side.BUY:
            return delta > 0 and recent[-1].ask >= recent[-2].ask
        return delta < 0 and recent[-1].bid <= recent[-2].bid

    def on_bar_close(self, candle: Candle, emit_signals: bool = True) -> tuple[StructureSnapshot, list[TradeSignal]]:
        """
        Process a confirmed closed M1 candle:
          1. Update internal + external market structure
          2. Evaluate existing pending setups (expiration, flip cancellation, rejection close, immediate)
          3. Detect new bullish/bearish wickless candles and register pending setups
        """
        snap = self.structure.update(candle)
        self.last_snapshot = snap
        bar_idx = snap.bar_index
        signals: list[TradeSignal] = []

        if not emit_signals:
            return snap, signals

        tol = self.cfg.tol_ticks * self.mintick
        min_body = self.cfg.min_body_ticks * self.mintick
        buf = self.cfg.sl_buffer_ticks * self.mintick
        pip = self._pip_size()
        zone_buf = self.cfg.zone_pips * pip

        body = abs(candle.close - candle.open)
        body_ok = body >= min_body and body > 0
        raw_bull = candle.close > candle.open and (candle.open - candle.low) <= tol and body_ok
        raw_bear = candle.close < candle.open and (candle.high - candle.open) <= tol and body_ok

        if raw_bull:
            self.bull_wickless_count += 1
        if raw_bear:
            self.bear_wickless_count += 1

        retest = self.cfg.entry_mode != "immediate"
        is_limit = self.cfg.entry_mode == "retest_limit"
        is_reject = self.cfg.entry_mode == "retest_rejection"
        is_immediate = self.cfg.entry_mode == "immediate"

        blocked_by_consol = self.cfg.block_in_consolidation and snap.consolidating

        # ── Step 1: Detect new setups on this confirmed bar ──
        if raw_bull and self.cfg.allow_longs and not blocked_by_consol:
            ext_ok = (not self.cfg.require_external_alignment) or (snap.external_trend == 1)
            l_sl: Optional[float] = None
            l_struct_tp: Optional[float] = None
            l_tag = ""
            if not self.cfg.use_structure:
                l_sl = candle.open - buf
                l_tag = "Wickless"
            elif ext_ok and snap.choch_on and snap.choch_dir == 1 and snap.choch_lvl is not None:
                l_sl = snap.choch_lvl - buf
                l_tag = "CHOCH"
            elif ext_ok and snap.internal_trend == 1:
                prov_l = self.structure.prov_low(snap.last_low_bar, bar_idx) if self.cfg.sl_live else None
                l_struct_tp = snap.last_hh if (snap.last_hh is not None and snap.last_hh > candle.close) else None
                if prov_l is not None and prov_l < candle.close:
                    l_sl = prov_l - buf
                elif snap.last_hl is not None and snap.last_hl < candle.close:
                    l_sl = snap.last_hl - buf
                elif snap.last_low is not None and snap.last_low < candle.close:
                    l_sl = snap.last_low - buf
                else:
                    l_sl = candle.open - buf
                l_tag = "Trend"

            l_ref = (candle.open + zone_buf) if retest else candle.close
            if l_sl is not None and l_sl < l_ref:
                l_entry: Optional[float] = None
                l_tp: Optional[float] = None
                l_ok = True
                if is_limit:
                    l_entry = candle.open + zone_buf
                    resolved = self._resolve_tp(Side.BUY, l_entry, l_sl, l_struct_tp)
                    if resolved is None:
                        l_ok = False
                    else:
                        l_tp, _ = resolved
                if l_ok:
                    self.pending_setups.append(
                        PendingSetup(
                            setup_id=uuid.uuid4().hex[:10],
                            side=Side.BUY,
                            created_bar=bar_idx,
                            created_time=candle.time,
                            zone_bottom=min(candle.open, candle.low),
                            zone_top=candle.open + zone_buf,
                            sl_price=l_sl,
                            struct_tp_price=l_struct_tp,
                            entry_price=l_entry,
                            tp_price=l_tp,
                            tag=l_tag,
                            entry_mode=self.cfg.entry_mode,
                            retest_bars=self.cfg.retest_bars,
                        )
                    )

        if raw_bear and self.cfg.allow_shorts and not blocked_by_consol:
            ext_ok = (not self.cfg.require_external_alignment) or (snap.external_trend == -1)
            s_sl: Optional[float] = None
            s_struct_tp: Optional[float] = None
            s_tag = ""
            if not self.cfg.use_structure:
                s_sl = candle.open + buf
                s_tag = "Wickless"
            elif ext_ok and snap.choch_on and snap.choch_dir == -1 and snap.choch_lvl is not None:
                s_sl = snap.choch_lvl + buf
                s_tag = "CHOCH"
            elif ext_ok and snap.internal_trend == -1:
                prov_h = self.structure.prov_high(snap.last_high_bar, bar_idx) if self.cfg.sl_live else None
                s_struct_tp = snap.last_ll if (snap.last_ll is not None and snap.last_ll < candle.close) else None
                if prov_h is not None and prov_h > candle.close:
                    s_sl = prov_h + buf
                elif snap.last_lh is not None and snap.last_lh > candle.close:
                    s_sl = snap.last_lh + buf
                elif snap.last_high is not None and snap.last_high > candle.close:
                    s_sl = snap.last_high + buf
                else:
                    s_sl = candle.open + buf
                s_tag = "Trend"

            s_ref = (candle.open - zone_buf) if retest else candle.close
            if s_sl is not None and s_sl > s_ref:
                s_entry: Optional[float] = None
                s_tp: Optional[float] = None
                s_ok = True
                if is_limit:
                    s_entry = candle.open - zone_buf
                    resolved = self._resolve_tp(Side.SELL, s_entry, s_sl, s_struct_tp)
                    if resolved is None:
                        s_ok = False
                    else:
                        s_tp, _ = resolved
                if s_ok:
                    self.pending_setups.append(
                        PendingSetup(
                            setup_id=uuid.uuid4().hex[:10],
                            side=Side.SELL,
                            created_bar=bar_idx,
                            created_time=candle.time,
                            zone_bottom=candle.open - zone_buf,
                            zone_top=max(candle.open, candle.high),
                            sl_price=s_sl,
                            struct_tp_price=s_struct_tp,
                            entry_price=s_entry,
                            tp_price=s_tp,
                            tag=s_tag,
                            entry_mode=self.cfg.entry_mode,
                            retest_bars=self.cfg.retest_bars,
                        )
                    )

        # ── Step 2: Evaluate pending setups on confirmed bar close ──
        remaining: list[PendingSetup] = []
        for setup in self.pending_setups:
            age = bar_idx - setup.created_bar
            trig = False
            dead = False

            if is_immediate and setup.entry_mode == "immediate":
                if age == 0:
                    trig = True
            elif age >= 1:
                broken = (
                    is_reject
                    and setup.entry_mode == "retest_rejection"
                    and (
                        (setup.side == Side.BUY and (candle.close < setup.zone_bottom or candle.low <= setup.sl_price))
                        or (setup.side == Side.SELL and (candle.close > setup.zone_top or candle.high >= setup.sl_price))
                    )
                )
                flipped = (
                    self.cfg.cancel_on_flip
                    and self.cfg.use_structure
                    and (
                        (setup.side == Side.BUY and snap.internal_trend == -1)
                        or (setup.side == Side.SELL and snap.internal_trend == 1)
                    )
                )
                touched = (
                    candle.low <= setup.zone_top
                    if setup.side == Side.BUY
                    else candle.high >= setup.zone_bottom
                )
                rejected = (
                    (not is_reject)
                    or (not self.cfg.need_reject)
                    or (candle.close > candle.open if setup.side == Side.BUY else candle.close < candle.open)
                )

                if broken:
                    dead = True
                elif touched and rejected:
                    # Note: for retest_limit in live tick mode, tick handler may already have filled & removed it.
                    # In bar-level backtesting (or if bar close completes retest), fire if still pending.
                    trig = True
                elif age >= setup.retest_bars or flipped:
                    dead = True

            if trig:
                entry = setup.entry_price if (setup.entry_mode == "retest_limit" and setup.entry_price is not None) else candle.close
                if setup.entry_mode == "retest_limit" and setup.tp_price is not None:
                    risk = (entry - setup.sl_price) if setup.side == Side.BUY else (setup.sl_price - entry)
                    tp = setup.tp_price
                    rr = abs(tp - entry) / risk if risk > 0 else 0.0
                    ok = risk > 0 and (self.cfg.tp_mode == "fixed_rr" or rr >= self.cfg.min_rr)
                else:
                    resolved = self._resolve_tp(setup.side, entry, setup.sl_price, setup.struct_tp_price)
                    if resolved is None:
                        ok = False
                        tp, rr = 0.0, 0.0
                    else:
                        tp, rr = resolved
                        ok = True

                if ok:
                    signals.append(
                        TradeSignal(
                            signal_id=setup.setup_id,
                            side=setup.side,
                            entry_price=entry,
                            sl_price=setup.sl_price,
                            tp_price=tp,
                            rr=rr,
                            tag=setup.tag,
                            source=setup.source,
                            created_time=candle.time,
                            is_immediate=(setup.entry_mode == "immediate"),
                        )
                    )
                else:
                    self.cancelled_setups_count += 1
            elif dead:
                self.cancelled_setups_count += 1
            else:
                remaining.append(setup)

        self.pending_setups = remaining
        return snap, signals

    def on_tick(self, tick: Tick) -> list[TradeSignal]:
        """
        Real-time tick-by-tick evaluation for pending 'retest_limit' setups so entries
        fire at the exact instant incoming market price touches the zone edge (with
        optional tick impulse confirmation), rather than waiting for bar close.
        """
        self.tick_buffer.append(tick)
        if not self.pending_setups:
            return []

        fired: list[TradeSignal] = []
        remaining: list[PendingSetup] = []
        for setup in self.pending_setups:
            if setup.entry_mode != "retest_limit" or setup.entry_price is None or setup.tp_price is None:
                remaining.append(setup)
                continue

            # Ensure we are past the signal candle (age >= 1 in live mode means after bar close)
            touched = (
                tick.ask <= setup.entry_price
                if setup.side == Side.BUY
                else tick.bid >= setup.entry_price
            )
            if touched and self._tick_impulse_confirmed(setup.side):
                exec_price = tick.ask if setup.side == Side.BUY else tick.bid
                risk = (exec_price - setup.sl_price) if setup.side == Side.BUY else (setup.sl_price - exec_price)
                if risk > 0:
                    tp = setup.tp_price
                    rr = abs(tp - exec_price) / risk
                    fired.append(
                        TradeSignal(
                            signal_id=setup.setup_id,
                            side=setup.side,
                            entry_price=exec_price,
                            sl_price=setup.sl_price,
                            tp_price=tp,
                            rr=rr,
                            tag=setup.tag,
                            source=setup.source,
                            created_time=tick.time,
                            is_immediate=False,
                        )
                    )
                    continue
            remaining.append(setup)

        self.pending_setups = remaining
        return fired
