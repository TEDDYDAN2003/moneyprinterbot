from __future__ import annotations

import asyncio
from dataclasses import replace
import html
import logging
from datetime import date, datetime, timezone
from typing import Callable, Optional

from bot.broker_interface import BrokerAdapter
from bot.external_bridge import ExternalSignalBridge
from bot.models import (
    BotConfig,
    BotRunState,
    Position,
    Side,
    SymbolSpec,
    Tick,
    TradeSignal,
)
from bot.market_intel import MarketIntelLogger
from bot.trade_learning_log import TradeLearningLogger, SRSnapshot
from bot.risk_manager import RiskManager
from bot.wickless_strategy import WicklessStructureStrategy
from bot.structure_momentum_strategy import StructureMomentumStrategy

logger = logging.getLogger(__name__)


class LiveTradingEngine:
    """
    Coordinates MT5 tick polling, M1 candle close detection, Wickless Structure analysis,
    External Indicator price watching, Risk/Scaling checks, and Dollar TP/SL auto-closing.
    """

    def __init__(
        self,
        cfg: BotConfig,
        broker: BrokerAdapter,
        dry_run: bool = False,
        notify_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.cfg = cfg
        self.broker = broker
        self.dry_run = dry_run
        self._notify = notify_callback or (lambda msg: None)

        self.run_state = BotRunState.PAUSED
        self.spec: Optional[SymbolSpec] = None
        self.strategy: Optional[WicklessStructureStrategy | StructureMomentumStrategy] = None
        self.bridge: Optional[ExternalSignalBridge] = None
        self.risk = RiskManager(cfg)
        self.market_intel = MarketIntelLogger(
            cfg.logging.market_intel_enabled,
            cfg.logging.market_intel_path,
        )
        self.learning = TradeLearningLogger(
            enabled=True,
            path="reports/trade_learning.jsonl",
        )

        self._last_closed_bar_time: Optional[datetime] = None
        self._current_utc_date: date = datetime.now(timezone.utc).date()
        self._daily_realized_pnl_usd: float = 0.0
        self._known_positions: dict[int, Position] = {}
        self._stop_event = asyncio.Event()

    def set_notifier(self, cb: Callable[[str], None]) -> None:
        self._notify = cb

    def _emit(self, text: str) -> None:
        logger.info(text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))
        self._notify(text)

    def initialize(self) -> None:
        if not self.broker.connect():
            raise RuntimeError("Failed to connect to MT5 Desktop terminal.")

        self.spec = self.broker.get_symbol_spec(self.cfg.mt5.symbol)
        if self.cfg.strategy.mode == "structure_momentum":
            self.strategy = StructureMomentumStrategy(
                cfg=self.cfg.strategy, symbol=self.spec.symbol, mintick=self.spec.point,
                sessions=self.cfg.sessions, max_spread_points=self.cfg.mt5.max_spread_points,
            )
        else:
            self.strategy = WicklessStructureStrategy(cfg=self.cfg.strategy, symbol=self.spec.symbol, mintick=self.spec.point)
        self.bridge = ExternalSignalBridge(
            cfg=self.cfg.external_bridge,
            symbol=self.spec.symbol,
            point=self.spec.point,
        )
        self.bridge.start_webhook_if_enabled()

        # Warm up M1 structure engine on historical closed bars (no trades emitted)
        candles = self.broker.get_m1_candles(self.spec.symbol, self.cfg.mt5.bars_history_count)
        if len(candles) > 1:
            closed_bars = candles[:-1]  # Exclude still-forming current candle
            for bar in closed_bars:
                self.strategy.on_bar_close(bar, emit_signals=False)
            self._last_closed_bar_time = closed_bars[-1].time

        for pos in self.broker.get_open_positions(self.spec.symbol, self.cfg.mt5.magic_number):
            self._known_positions[pos.ticket] = pos

    def set_running(self) -> str:
        self.run_state = BotRunState.RUNNING
        return (
            "<b>Bot STARTED</b> — Actively reading M1 structure &amp; live ticks.\n"
            f"Mode: <code>{self.cfg.strategy.mode}</code> | Symbol: <code>{self.cfg.mt5.symbol}</code>\n"
            f"Dollar TP: <code>${self.cfg.dollar_exits.trade_tp_usd:.2f}</code> | "
            f"Basket TP: <code>${self.cfg.dollar_exits.basket_tp_usd:.2f}</code>"
        )

    def set_paused(self) -> str:
        self.run_state = BotRunState.PAUSED
        return "<b>Bot PAUSED</b> — No new entries will be opened; existing risk/dollar exits remain active."

    def emergency_close_all(self, reason: str = "MANUAL_CLOSEALL") -> int:
        self.run_state = BotRunState.PAUSED
        if self.spec is None:
            return 0
        positions = self.broker.get_open_positions(self.spec.symbol, self.cfg.mt5.magic_number)
        closed = 0
        for pos in positions:
            if self.dry_run:
                closed += 1
                continue
            if self.broker.close_position(pos, self.cfg.mt5.slippage_points, reason):
                closed += 1
                self._daily_realized_pnl_usd += pos.net_pnl_usd
                self.risk.record_trade_close(datetime.now(timezone.utc), pos.net_pnl_usd)
                self._known_positions.pop(pos.ticket, None)
        self._emit(f"<b>{html.escape(reason)}</b>: Closed {closed} position(s). Bot is now PAUSED.")
        return closed

    def _roll_daily_counters_if_needed(self, now_utc: datetime) -> None:
        today = now_utc.date()
        if today != self._current_utc_date:
            self._current_utc_date = today
            self._daily_realized_pnl_usd = 0.0
            if self.run_state in (BotRunState.HALTED_DAILY_PROFIT, BotRunState.HALTED_DAILY_LOSS):
                self.run_state = BotRunState.RUNNING
                self._emit("New UTC day started — Daily PnL counters reset and bot resumed.")


    def _current_sr_snapshot(self) -> SRSnapshot:
        """Extracts current SR context from strategy if available."""
        sr_dict = getattr(self.strategy, "_last_sr", {})
        return SRSnapshot(
            sr_low=sr_dict.get("sr_low"),
            sr_high=sr_dict.get("sr_high"),
            pos_ratio=sr_dict.get("pos_ratio", 0.5),
            zone_state=sr_dict.get("zone_state", "IN_BETWEEN"),
            consol_detected=sr_dict.get("consol_detected", False),
            consol_range_points=sr_dict.get("consol_range_points", 0.0),
        )

    def _execute_signal(self, sig: TradeSignal, tick: Tick) -> None:
        assert self.spec is not None
        pace = getattr(self.cfg.strategy, "pace_mode", "fast")
        batch_size = 6 if pace == "fast" else 3 if pace == "medium" else 1

        open_positions = self.broker.get_open_positions(self.spec.symbol, self.cfg.mt5.magic_number)
        account = self.broker.get_account_info()

        allowed, lots, reason = self.risk.can_open_trade(
            signal=sig,
            open_positions=open_positions,
            account=account,
            spec=self.spec,
            tick=tick,
            run_state=self.run_state,
        )
        if not allowed:
            logger.info("Signal %s skipped (%s): %s", sig.signal_id, sig.side.value, reason)
            return

        if self.cfg.strategy.target_tp_usd > 0 and self.cfg.strategy.target_sl_usd > 0 and lots > 0:
            usd_per_price_unit = (
                (1.0 / self.spec.trade_tick_size) * self.spec.trade_tick_value * lots
            )
            if usd_per_price_unit > 0:
                spread = tick.ask - tick.bid
                tp_dist = (self.cfg.strategy.target_tp_usd / usd_per_price_unit) + spread
                sl_dist = self.cfg.strategy.target_sl_usd / usd_per_price_unit
                entry = tick.ask if sig.side == Side.BUY else tick.bid
                tp_price = round(entry + tp_dist if sig.side == Side.BUY else entry - tp_dist, self.spec.digits)
                sl_price = round(entry - sl_dist if sig.side == Side.BUY else entry + sl_dist, self.spec.digits)
                rr = tp_dist / sl_dist if sl_dist > 0 else 0.0
                sig = replace(sig, entry_price=entry, tp_price=tp_price, sl_price=sl_price, rr=rr)

        if self.dry_run:
            self._emit(
                f"<b>[DRY RUN - {pace.upper()}] {batch_size}x {sig.side.value} {self.spec.symbol}</b>\n"
                f"Lots: <code>{lots:.2f}</code> each | Entry: <code>{sig.entry_price:.2f}</code>\n"
                f"SL: <code>{sig.sl_price:.2f}</code> | TP: <code>{sig.tp_price:.2f}</code> (RR {sig.rr:.2f})\n"
                f"Tag: <code>{html.escape(sig.tag)}</code>"
            )
            return

        executed_tickets: list[int] = []
        for idx in range(batch_size):
            cur_open = self.broker.get_open_positions(self.spec.symbol, self.cfg.mt5.magic_number)
            can_trade, cur_lots, _ = self.risk.can_open_trade(
                signal=sig,
                open_positions=cur_open,
                account=account,
                spec=self.spec,
                tick=tick,
                run_state=self.run_state,
            )
            if not can_trade:
                break
            ticket = self.broker.open_market_order(
                symbol=self.spec.symbol,
                side=sig.side,
                volume=cur_lots,
                sl=sig.sl_price,
                tp=sig.tp_price,
                magic=self.cfg.mt5.magic_number,
                deviation=self.cfg.mt5.slippage_points,
                comment=f"WS_{sig.tag}_{idx+1}",
            )
            if ticket is not None:
                executed_tickets.append(ticket)
                self.risk.record_trade_open(tick.time)
                # Trade Learning: record open with full market context
                try:
                    candles = self.broker.get_m1_candles(self.spec.symbol, 2)
                    last_c = candles[-2] if len(candles) >= 2 else (candles[-1] if candles else None)
                    if last_c is not None:
                        self.learning.log_open(
                            ticket=ticket,
                            signal=sig,
                            lots=cur_lots,
                            candle=last_c,
                            structure_snap=getattr(self.strategy, "last_snapshot", None),
                            sr_snap=self._current_sr_snapshot(),
                            spread_points=tick.spread_points(self.spec.point),
                            pace_mode=pace,
                            cfg=self.cfg,
                        )
                except Exception as _learn_err:
                    logger.debug("TradeLearning open log error: %s", _learn_err)

        if executed_tickets:
            if self.cfg.telegram.notify_fills_and_closes:
                self._emit(
                    f"<b>EXECUTED {len(executed_tickets)}x {sig.side.value} {self.spec.symbol} ({pace.upper()} mode)</b>\n"
                    f"Tickets: <code>{executed_tickets}</code>\n"
                    f"Lots: <code>{lots:.2f}</code> each | Fill ~<code>{sig.entry_price:.2f}</code>\n"
                    f"SL: <code>{sig.sl_price:.2f}</code> | TP: <code>{sig.tp_price:.2f}</code> (RR {sig.rr:.2f})\n"
                    f"Spread: <code>{tick.spread_points(self.spec.point):.1f} pts</code> | "
                    f"Dollar TP: <code>${self.cfg.dollar_exits.trade_tp_usd:.2f}</code>"
                )
        else:
            self._emit(f"Order failed for {sig.side.value} {self.spec.symbol} ({lots:.2f} lots).")

    def _manage_open_positions(self, tick: Tick) -> None:
        assert self.spec is not None and self.strategy is not None
        open_positions = self.broker.get_open_positions(self.spec.symbol, self.cfg.mt5.magic_number)

        # Detect positions closed naturally by broker SL/TP since last poll
        current_tickets = {p.ticket for p in open_positions}
        for old_ticket, old_pos in list(self._known_positions.items()):
            if old_ticket not in current_tickets:
                self._daily_realized_pnl_usd += old_pos.net_pnl_usd
                self.risk.record_trade_close(tick.time, old_pos.net_pnl_usd)
                # Trade Learning: record natural close (broker SL or TP hit)
                exit_price = tick.bid if old_pos.side == Side.BUY else tick.ask
                nat_reason = "BROKER_TP" if old_pos.net_pnl_usd > 0 else "BROKER_SL"
                self.learning.log_close(
                    ticket=old_ticket,
                    pnl_usd=old_pos.net_pnl_usd,
                    exit_price=exit_price,
                    exit_reason=nat_reason,
                    sr_snap=self._current_sr_snapshot(),
                    cfg=self.cfg,
                )
                if self.cfg.telegram.notify_fills_and_closes:
                    self._emit(
                        f"<b>Position Closed (#{old_ticket})</b> {old_pos.side.value} {old_pos.volume:.2f} lots\n"
                        f"Last Recorded PnL: <code>${old_pos.net_pnl_usd:+.2f}</code>"
                    )
                self._known_positions.pop(old_ticket, None)
        for p in open_positions:
            self._known_positions[p.ticket] = p

        # 1. Tick-monitored Dollar TP / Dollar SL / Daily Circuit Breakers
        exit_dec = self.risk.evaluate_dollar_exits(
            open_positions, self._daily_realized_pnl_usd, tick.time
        )
        if exit_dec.close_all_reason:
            for pos in open_positions:
                if not self.dry_run and self.broker.close_position(
                    pos, self.cfg.mt5.slippage_points, exit_dec.close_all_reason
                ):
                    self._daily_realized_pnl_usd += pos.net_pnl_usd
                    self.risk.record_trade_close(tick.time, pos.net_pnl_usd)
                    self._known_positions.pop(pos.ticket, None)
            if exit_dec.halt_state is not None:
                self.run_state = exit_dec.halt_state
            self._emit(
                f"<b>{html.escape(exit_dec.close_all_reason)}</b>\n"
                f"Closed {len(open_positions)} position(s). State: <code>{self.run_state.value}</code>"
            )
            return

        if exit_dec.close_tickets:
            pos_map = {p.ticket: p for p in open_positions}
            for ticket, reason in exit_dec.close_tickets:
                pos = pos_map.get(ticket)
                if pos is None:
                    continue
                if not self.dry_run and self.broker.close_position(
                    pos, self.cfg.mt5.slippage_points, reason
                ):
                    self._daily_realized_pnl_usd += pos.net_pnl_usd
                    self.risk.record_trade_close(tick.time, pos.net_pnl_usd)
                    self._known_positions.pop(ticket, None)
                    # Trade Learning: record auto-closed trade
                    exit_price = tick.bid if pos.side == Side.BUY else tick.ask
                    self.learning.log_close(
                        ticket=ticket,
                        pnl_usd=pos.net_pnl_usd,
                        exit_price=exit_price,
                        exit_reason=reason,
                        sr_snap=self._current_sr_snapshot(),
                        cfg=self.cfg,
                    )
                    self._emit(
                        f"<b>Auto-Closed #{ticket}</b> ({html.escape(reason)})\n"
                        f"Side: {pos.side.value} {pos.volume:.2f} lots | PnL: <code>${pos.net_pnl_usd:+.2f}</code>"
                    )

        # 2. Breakeven & ATR Trailing Stops
        atr_val = self.strategy.last_snapshot.atr_14 if self.strategy.last_snapshot else 0.0
        mods = self.risk.evaluate_trailing_stops(open_positions, tick, atr_val, self.spec)
        pos_map = {p.ticket: p for p in open_positions}
        for mod in mods:
            pos = pos_map.get(mod.ticket)
            if pos and not self.dry_run:
                self.broker.modify_position_sl_tp(mod.ticket, pos.symbol, mod.new_sl, pos.tp)

    def step_once(self) -> None:
        """Single synchronous tick + bar-close evaluation cycle."""
        assert self.spec is not None and self.strategy is not None and self.bridge is not None
        tick = self.broker.get_latest_tick(self.spec.symbol)
        if tick is None:
            return

        self._roll_daily_counters_if_needed(tick.time)
        self._manage_open_positions(tick)

        # Check newly closed M1 bar(s)
        candles = self.broker.get_m1_candles(self.spec.symbol, 5)
        if len(candles) >= 2:
            latest_closed = candles[-2]
            if self._last_closed_bar_time is None or latest_closed.time > self._last_closed_bar_time:
                self._last_closed_bar_time = latest_closed.time
                if self.cfg.strategy.mode in ("wickless_structure", "structure_momentum", "hybrid"):
                    skip_start = len(getattr(self.strategy, "skip_reasons", []))
                    snap, bar_signals = self.strategy.on_bar_close(latest_closed, emit_signals=True)
                    skip_reasons = getattr(self.strategy, "skip_reasons", [])[skip_start:]
                    decision_reasons = list(skip_reasons)
                    if not bar_signals and not decision_reasons:
                        if snap.consolidating:
                            decision_reasons.append("consolidation")
                        elif not snap.event_note:
                            decision_reasons.append("no_confirmed_structure_event")
                        else:
                            decision_reasons.append("no_entry_setup")
                    htf = {}
                    if isinstance(self.strategy, StructureMomentumStrategy):
                        htf = {
                            str(minutes): {
                                "internal": snapshot.internal_trend,
                                "external": snapshot.external_trend,
                            }
                            for minutes, snapshot in self.strategy.htf_snapshots.items()
                        }
                    self.market_intel.write(
                        {
                            "time": latest_closed.time.isoformat(),
                            "symbol": self.spec.symbol,
                            "ohlc": {
                                "open": latest_closed.open,
                                "high": latest_closed.high,
                                "low": latest_closed.low,
                                "close": latest_closed.close,
                            },
                            "spread_points": tick.spread_points(self.spec.point),
                            "m1": {
                                "internal_trend": snap.internal_trend,
                                "external_trend": snap.external_trend,
                                "event": snap.event_note,
                                "atr_14": snap.atr_14,
                                "consolidating": snap.consolidating,
                            },
                            "htf": htf,
                            "sr_zone": getattr(self.strategy, "_last_sr", {}),
                            "pending_setups": len(getattr(self.strategy, "pending_setups", [])),
                            "skips": decision_reasons,
                            "signals": [
                                {
                                    "id": signal.signal_id,
                                    "side": signal.side.value,
                                    "tag": signal.tag,
                                    "entry": signal.entry_price,
                                    "sl": signal.sl_price,
                                    "tp": signal.tp_price,
                                }
                                for signal in bar_signals
                            ],
                        }
                    )
                    if snap.event_note and self.cfg.telegram.notify_structure_shifts:
                        self._emit(
                            f"<b>Structure Shift ({self.spec.symbol} M1)</b>: <code>{snap.event_note}</code>\n"
                            f"Int Trend: <code>{snap.internal_trend}</code> | Ext Trend: <code>{snap.external_trend}</code>"
                        )
                    for sig in bar_signals:
                        self._execute_signal(sig, tick)

        # Tick-level entry execution (retest_limit zone touches + external indicator price levels)
        if self.cfg.strategy.mode in ("wickless_structure", "structure_momentum", "hybrid"):
            for sig in self.strategy.on_tick(tick):
                self._execute_signal(sig, tick)

        if self.cfg.strategy.mode in ("external_bridge", "hybrid"):
            for sig in self.bridge.evaluate_on_tick(tick):
                self._execute_signal(sig, tick)

    async def run_loop(self) -> None:
        self.initialize()
        poll_sec = max(0.05, self.cfg.mt5.tick_poll_interval_ms / 1000.0)
        logger.info("LiveTradingEngine started (%s, poll=%.2fs).", self.cfg.mt5.symbol, poll_sec)
        while not self._stop_event.is_set():
            try:
                await asyncio.to_thread(self.step_once)
            except Exception as exc:
                logger.exception("Error in trading engine loop: %s", exc)
            await asyncio.sleep(poll_sec)

    def stop(self) -> None:
        self._stop_event.set()
        if self.bridge:
            self.bridge.stop()
        self.broker.disconnect()

    def format_startup_html(self) -> str:
        assert self.spec is not None
        acc = self.broker.get_account_info()
        return (
            "<b>XAUUSDm Wickless Structure Bot Online</b>\n"
            f"State: <code>{self.run_state.value}</code> | Dry Run: <code>{self.dry_run}</code>\n"
            f"Symbol: <code>{self.spec.symbol}</code> | Balance: <code>${acc.balance:,.2f}</code>\n"
            f"Strategy: <code>{self.cfg.strategy.mode}</code> ({self.cfg.strategy.entry_mode})\n"
            f"Dollar TP: <code>${self.cfg.dollar_exits.trade_tp_usd:.2f}</code> | "
            f"Basket TP: <code>${self.cfg.dollar_exits.basket_tp_usd:.2f}</code>\n"
            "Send /run when ready to begin live execution."
        )

    def format_status_html(self) -> str:
        assert self.spec is not None and self.strategy is not None
        acc = self.broker.get_account_info()
        tick = self.broker.get_latest_tick(self.spec.symbol)
        positions = self.broker.get_open_positions(self.spec.symbol, self.cfg.mt5.magic_number)
        snap = self.strategy.last_snapshot
        floating = sum(p.net_pnl_usd for p in positions)
        spread_str = f"{tick.spread_points(self.spec.point):.1f} pts" if tick else "N/A"
        price_str = f"{tick.bid:.2f} / {tick.ask:.2f}" if tick else "N/A"

        trend_map = {1: "Bullish", -1: "Bearish", 0: "Neutral"}
        int_tr = trend_map.get(snap.internal_trend, "Neutral") if snap else "Warming"
        ext_tr = trend_map.get(snap.external_trend, "Neutral") if snap else "Warming"

        pos_lines = "\n".join(
            f"• #{p.ticket} {p.side.value} {p.volume:.2f} @ {p.open_price:.2f} | PnL: <code>${p.net_pnl_usd:+.2f}</code>"
            for p in positions
        ) or "None"

        return (
            f"<b>Status — {self.spec.symbol}</b> ({self.run_state.value})\n"
            f"Balance: <code>${acc.balance:,.2f}</code> | Equity: <code>${acc.equity:,.2f}</code>\n"
            f"Quote: <code>{price_str}</code> | Spread: <code>{spread_str}</code>\n"
            f"Internal Trend: <code>{int_tr}</code> | External Trend: <code>{ext_tr}</code>\n"
            f"Pending Retests: <code>{len(self.strategy.pending_setups)}</code> | "
            f"Daily Realized: <code>${self._daily_realized_pnl_usd:+.2f}</code>\n"
            f"Open Floating PnL: <code>${floating:+.2f}</code>\n\n"
            f"<b>Open Positions ({len(positions)}):</b>\n{pos_lines}"
        )

    def format_params_html(self) -> str:
        s = self.cfg.strategy
        r = self.cfg.risk
        d = self.cfg.dollar_exits
        return (
            "<b>Active Parameters</b>\n"
            f"Strategy: <code>{s.mode}</code> | Entry: <code>{s.entry_mode}</code>\n"
            f"Tol Ticks: <code>{s.tol_ticks}</code> | Zone Pips: <code>{s.zone_pips}</code> | Retest Bars: <code>{s.retest_bars}</code>\n"
            f"Int/Ext Len: <code>{s.internal_len}/{s.external_len}</code> | R:R: <code>{s.rr_value}</code>\n"
            f"Sizing: <code>{r.sizing_mode}</code> (Risk {r.risk_per_trade_pct}% / Fixed {r.fixed_lot})\n"
            f"Scaling: <code>{r.allow_scaling}</code> (Max {r.max_open_positions} pos, {r.max_total_lots} lots, {r.max_total_risk_pct}% risk)\n"
            f"Dollar TP/SL: <code>+${d.trade_tp_usd:.2f}</code> / <code>-${d.trade_sl_usd:.2f}</code>\n"
            f"Basket TP/SL: <code>+${d.basket_tp_usd:.2f}</code> / <code>-${d.basket_sl_usd:.2f}</code>\n"
            f"Daily Caps: <code>+${d.daily_max_profit_usd:.2f}</code> / <code>-${d.daily_max_loss_usd:.2f}</code>"
        )
