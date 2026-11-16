from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from bot.models import (
    AccountInfo,
    BotConfig,
    BotRunState,
    Position,
    Side,
    SymbolSpec,
    Tick,
    TradeSignal,
)


@dataclass
class ExitDecision:
    close_tickets: list[tuple[int, str]] = field(default_factory=list)
    close_all_reason: Optional[str] = None
    halt_state: Optional[BotRunState] = None


@dataclass
class SLModification:
    ticket: int
    new_sl: float
    reason: str


class RiskManager:
    """
    Handles spread gating, session filtering, dynamic MT5 lot sizing,
    multi-entry scaling (pyramiding) risk caps, trailing/breakeven stops,
    and real-time tick-monitored USD profit/loss auto-closing.
    """

    def __init__(self, cfg: BotConfig) -> None:
        self.cfg = cfg
        self._counter_date = None
        self._trades_today = 0
        self._consecutive_losses = 0
        self._winning_streak = 0
        self._cooldown_until: Optional[datetime] = None
        self._pnl_signs: dict[int, int] = {}
        self._pnl_flips: dict[int, int] = {}

    def _roll_counters(self, now_utc: datetime) -> None:
        if self._counter_date != now_utc.date():
            self._counter_date = now_utc.date()
            self._trades_today = 0
            self._consecutive_losses = 0
            self._winning_streak = 0
            self._cooldown_until = None

    def record_trade_open(self, opened_at: datetime) -> None:
        self._roll_counters(opened_at)
        self._trades_today += 1

    def record_trade_close(self, closed_at: datetime, pnl_usd: float) -> None:
        self._roll_counters(closed_at)
        if pnl_usd < 0:
            self._consecutive_losses += 1
            self._winning_streak = 0
            cooldown = self.cfg.risk.loss_cooldown_minutes
            if cooldown > 0:
                self._cooldown_until = closed_at + timedelta(minutes=cooldown)
        elif pnl_usd > 0:
            self._consecutive_losses = 0
            self._winning_streak += 1
            self._cooldown_until = None

    def _scaled_fixed_lot(self) -> float:
        base_lot = self.cfg.risk.fixed_lot
        if not self.cfg.risk.profit_streak_scaling_enabled:
            return base_lot
        step_wins = max(1, self.cfg.risk.profit_streak_step_wins)
        increments = self._winning_streak // step_wins
        return min(
            base_lot + increments * self.cfg.risk.profit_streak_add_lots,
            self.cfg.risk.profit_streak_max_lot,
        )

    def _effective_max_open_positions(self) -> int:
        configured_max = max(1, self.cfg.risk.max_open_positions)
        if not self.cfg.risk.profit_streak_scaling_enabled:
            return configured_max
        step_wins = max(1, self.cfg.risk.profit_streak_positions_step_wins)
        increments = self._winning_streak // step_wins
        scaled_max = self.cfg.risk.profit_streak_positions_start + (
            increments * self.cfg.risk.profit_streak_positions_add
        )
        return min(
            configured_max,
            self.cfg.risk.profit_streak_positions_max,
            max(1, scaled_max),
        )

    def is_within_session(self, now_utc: datetime) -> bool:
        if not self.cfg.sessions.enabled:
            return True
        hour = now_utc.hour
        for start_h, end_h in self.cfg.sessions.allowed_utc_hours:
            if start_h <= end_h:
                if start_h <= hour < end_h:
                    return True
            else:
                if hour >= start_h or hour < end_h:
                    return True
        return False

    def is_spread_acceptable(self, tick: Tick, spec: SymbolSpec) -> bool:
        return tick.spread_points(spec.point) <= float(self.cfg.mt5.max_spread_points)

    def normalize_volume(self, raw_lots: float, spec: SymbolSpec) -> float:
        step = spec.volume_step if spec.volume_step > 0 else 0.01
        min_v = max(spec.volume_min, self.cfg.risk.min_lot)
        max_v = min(spec.volume_max, self.cfg.risk.max_lot_per_trade)
        clamped = max(min_v, min(max_v, raw_lots))
        steps = math.floor(clamped / step + 1e-9)
        rounded = round(steps * step, 4)
        return max(min_v, min(max_v, rounded))

    def calculate_lot_size(
        self,
        account: AccountInfo,
        spec: SymbolSpec,
        entry_price: float,
        sl_price: float,
        risk_pct_override: Optional[float] = None,
    ) -> float:
        if self.cfg.risk.sizing_mode == "fixed_lot":
            return self.normalize_volume(self.cfg.risk.fixed_lot, spec)

        sl_dist = abs(entry_price - sl_price)
        if sl_dist <= 0 or spec.trade_tick_size <= 0 or spec.trade_tick_value <= 0:
            return self.normalize_volume(self.cfg.risk.min_lot, spec)

        risk_pct = risk_pct_override if risk_pct_override is not None else self.cfg.risk.risk_per_trade_pct
        risk_usd = account.equity * (risk_pct / 100.0)
        loss_per_lot = (sl_dist / spec.trade_tick_size) * spec.trade_tick_value
        if loss_per_lot <= 0:
            return self.normalize_volume(self.cfg.risk.min_lot, spec)

        raw_lots = risk_usd / loss_per_lot
        return self.normalize_volume(raw_lots, spec)

    def _estimate_position_risk_pct(
        self,
        pos: Position,
        account: AccountInfo,
        spec: SymbolSpec,
    ) -> float:
        if account.equity <= 0 or spec.trade_tick_size <= 0 or spec.trade_tick_value <= 0:
            return 0.0
        # If SL has moved into profit (locked), open risk is 0%
        if pos.side == Side.BUY and pos.sl >= pos.open_price:
            return 0.0
        if pos.side == Side.SELL and pos.sl > 0 and pos.sl <= pos.open_price:
            return 0.0
        if pos.sl <= 0:
            return self.cfg.risk.risk_per_trade_pct
        sl_dist = abs(pos.open_price - pos.sl)
        usd_risk = (sl_dist / spec.trade_tick_size) * spec.trade_tick_value * pos.volume
        return (usd_risk / account.equity) * 100.0

    def can_open_trade(
        self,
        signal: TradeSignal,
        open_positions: list[Position],
        account: AccountInfo,
        spec: SymbolSpec,
        tick: Tick,
        run_state: BotRunState,
    ) -> tuple[bool, float, str]:
        if run_state != BotRunState.RUNNING:
            return False, 0.0, f"Bot state is {run_state.value}"

        self._roll_counters(tick.time)
        max_trades = self.cfg.risk.max_trades_per_day
        if max_trades > 0 and self._trades_today >= max_trades:
            return False, 0.0, f"Daily trade cap reached ({max_trades})"
        max_losses = self.cfg.risk.max_consecutive_losses
        if max_losses > 0 and self._consecutive_losses >= max_losses:
            return False, 0.0, f"Consecutive-loss cooldown active ({max_losses} losses)"
        if self._cooldown_until is not None and tick.time < self._cooldown_until:
            return False, 0.0, f"Loss cooldown active until {self._cooldown_until.isoformat()}"

        if not self.is_within_session(tick.time):
            return False, 0.0, "Outside configured trading session hours"

        if not self.is_spread_acceptable(tick, spec):
            sp = tick.spread_points(spec.point)
            return False, 0.0, f"Spread too wide ({sp:.1f} > {self.cfg.mt5.max_spread_points} pts)"

        # Prevent opposing positions simultaneously
        for pos in open_positions:
            if pos.side != signal.side:
                return False, 0.0, "Opposing position already open"

        if open_positions:
            if not self.cfg.risk.allow_scaling:
                return False, 0.0, "Position already open and scaling disabled"

            effective_max = self._effective_max_open_positions()
            if len(open_positions) >= effective_max:
                return False, 0.0, f"Max open positions reached ({effective_max})"

            if self.cfg.risk.scale_only_in_profit:
                for pos in open_positions:
                    init_dist = abs(pos.open_price - pos.sl) if pos.sl > 0 else 0.0
                    cur_price = tick.bid if pos.side == Side.BUY else tick.ask
                    gain = (cur_price - pos.open_price) if pos.side == Side.BUY else (pos.open_price - cur_price)
                    if init_dist > 0:
                        r_now = gain / init_dist
                        if r_now < self.cfg.risk.scale_min_profit_r:
                            return (
                                False,
                                0.0,
                                f"Scale blocked: existing ticket {pos.ticket} at {r_now:.2f}R < {self.cfg.risk.scale_min_profit_r:.2f}R",
                            )
                    elif pos.net_pnl_usd <= 0:
                        return False, 0.0, "Scale blocked: existing trade not in profit"

        lots = self.calculate_lot_size(account, spec, signal.entry_price, signal.sl_price)
        if self.cfg.risk.sizing_mode == "fixed_lot":
            lots = self.normalize_volume(self._scaled_fixed_lot(), spec)
        if signal.tag in {"M1_FALLBACK", "M1_COUNTERTREND", "M1_EXPLORATION"}:
            lots = min(lots, self.cfg.strategy.m1_fallback_max_lot)
            lots = self.normalize_volume(lots, spec)
        current_lots = sum(p.volume for p in open_positions)
        remaining_lot_budget = self.cfg.risk.max_total_lots - current_lots
        if remaining_lot_budget < spec.volume_min:
            return False, 0.0, f"Max total lots cap reached ({self.cfg.risk.max_total_lots:.2f})"
        if lots > remaining_lot_budget:
            lots = self.normalize_volume(remaining_lot_budget, spec)

        # Enforce total portfolio open risk cap
        current_risk_pct = sum(self._estimate_position_risk_pct(p, account, spec) for p in open_positions)
        remaining_risk_pct = self.cfg.risk.max_total_risk_pct - current_risk_pct
        if remaining_risk_pct <= 0.05:
            return False, 0.0, f"Max total open risk cap reached ({self.cfg.risk.max_total_risk_pct:.2f}%)"

        if self.cfg.risk.sizing_mode == "risk_percent" and self.cfg.risk.risk_per_trade_pct > remaining_risk_pct:
            lots = self.calculate_lot_size(
                account,
                spec,
                signal.entry_price,
                signal.sl_price,
                risk_pct_override=remaining_risk_pct,
            )

        sl_dist = abs(signal.entry_price - signal.sl_price)
        if account.equity > 0 and spec.trade_tick_size > 0 and spec.trade_tick_value > 0 and sl_dist > 0:
            candidate_risk_pct = (
                (sl_dist / spec.trade_tick_size) * spec.trade_tick_value * lots
                / account.equity
                * 100.0
            )
            if candidate_risk_pct > remaining_risk_pct:
                lots = self.calculate_lot_size(
                    account,
                    spec,
                    signal.entry_price,
                    signal.sl_price,
                    risk_pct_override=remaining_risk_pct,
                )
                candidate_risk_pct = (
                    (sl_dist / spec.trade_tick_size) * spec.trade_tick_value * lots
                    / account.equity
                    * 100.0
                )
                if candidate_risk_pct > remaining_risk_pct + 1e-9:
                    return False, 0.0, "Scaled lot exceeds maximum total open risk cap"

        if lots < spec.volume_min:
            return False, 0.0, "Calculated lot size below broker minimum"

        return True, lots, "OK"

    def evaluate_dollar_exits(
        self,
        open_positions: list[Position],
        daily_realized_pnl_usd: float,
        now_utc: Optional[datetime] = None,
    ) -> ExitDecision:
        """
        Tick-level USD profit/loss auto-close evaluator.
        Handles:
          - Daily profit/loss circuit breakers
          - Whole-basket USD TP (e.g. +$15.00 across stacked entries) & USD SL
          - Individual trade USD TP (e.g. +$15.00) & USD SL
        """
        decision = ExitDecision()
        d_cfg = self.cfg.dollar_exits
        if not d_cfg.enabled:
            return decision

        floating_total = sum(p.net_pnl_usd for p in open_positions)
        daily_total = daily_realized_pnl_usd + floating_total

        if d_cfg.daily_max_profit_usd > 0 and daily_total >= d_cfg.daily_max_profit_usd:
            decision.close_all_reason = f"DAILY_PROFIT_CAP (+${daily_total:.2f})"
            decision.halt_state = BotRunState.HALTED_DAILY_PROFIT
            return decision

        if d_cfg.daily_max_loss_usd > 0 and daily_total <= -abs(d_cfg.daily_max_loss_usd):
            decision.close_all_reason = f"DAILY_LOSS_CAP (-${abs(daily_total):.2f})"
            decision.halt_state = BotRunState.HALTED_DAILY_LOSS
            return decision

        if not open_positions:
            return decision

        now = now_utc or max(
            (p.open_time for p in open_positions if p.open_time is not None),
            default=datetime.now(timezone.utc),
        )

        # Basket-level USD TP / SL
        if d_cfg.basket_tp_usd > 0 and floating_total >= d_cfg.basket_tp_usd:
            decision.close_all_reason = f"BASKET_DOLLAR_TP (+${floating_total:.2f} >= +${d_cfg.basket_tp_usd:.2f})"
            return decision

        if d_cfg.basket_sl_usd > 0 and floating_total <= -abs(d_cfg.basket_sl_usd):
            decision.close_all_reason = f"BASKET_DOLLAR_SL (-${abs(floating_total):.2f} <= -${abs(d_cfg.basket_sl_usd):.2f})"
            return decision

        # Per-trade USD TP / SL
        for pos in open_positions:
            pnl = pos.net_pnl_usd
            tp_target = (
                d_cfg.trade_tp_usd_per_lot * pos.volume
                if d_cfg.trade_tp_usd_per_lot > 0
                else d_cfg.trade_tp_usd
            )
            sl_target = (
                d_cfg.trade_sl_usd_per_lot * pos.volume
                if d_cfg.trade_sl_usd_per_lot > 0
                else d_cfg.trade_sl_usd
            )
            if tp_target > 0 and pnl >= tp_target:
                decision.close_tickets.append(
                    (pos.ticket, f"DOLLAR_TP (+${pnl:.2f} >= +${tp_target:.2f})")
                )
            elif sl_target > 0 and pnl <= -abs(sl_target):
                decision.close_tickets.append(
                    (pos.ticket, f"DOLLAR_SL (-${abs(pnl):.2f} <= -${abs(sl_target):.2f})")
                )
                continue

            if d_cfg.stagnation_enabled and pos.open_time is not None:
                sign = 1 if pnl > 0 else -1 if pnl < 0 else 0
                previous_sign = self._pnl_signs.get(pos.ticket)
                if sign and previous_sign and sign != previous_sign:
                    self._pnl_flips[pos.ticket] = self._pnl_flips.get(pos.ticket, 0) + 1
                if sign:
                    self._pnl_signs[pos.ticket] = sign
                age_minutes = (now - pos.open_time).total_seconds() / 60.0
                flips = self._pnl_flips.get(pos.ticket, 0)
                if (
                    age_minutes >= d_cfg.stagnation_min_minutes
                    and flips >= d_cfg.stagnation_flip_count
                    and abs(pnl) <= d_cfg.stagnation_pnl_band_usd
                ):
                    decision.close_tickets.append(
                        (pos.ticket, f"STAGNATION_EXIT ({age_minutes:.1f}m, {flips} PnL flips)")
                    )

        return decision

    def evaluate_trailing_stops(
        self,
        open_positions: list[Position],
        tick: Tick,
        atr_14: float,
        spec: SymbolSpec,
    ) -> list[SLModification]:
        mods: list[SLModification] = []
        be_r = self.cfg.risk.move_to_be_at_r
        trail_mult = self.cfg.risk.trailing_atr_mult
        if be_r <= 0 and trail_mult <= 0:
            return mods

        min_stop_dist = max(spec.stops_level * spec.point, spec.point * 2)

        for pos in open_positions:
            cur_price = tick.bid if pos.side == Side.BUY else tick.ask
            init_risk = abs(pos.open_price - pos.sl) if pos.sl > 0 else 0.0
            desired_sl: Optional[float] = None
            reason = ""

            if be_r > 0 and init_risk > 0:
                gain = (cur_price - pos.open_price) if pos.side == Side.BUY else (pos.open_price - cur_price)
                if gain >= init_risk * be_r:
                    be_level = pos.open_price + (spec.point if pos.side == Side.BUY else -spec.point)
                    if pos.side == Side.BUY and pos.sl < be_level:
                        desired_sl = be_level
                        reason = f"Breakeven (+{be_r:.1f}R)"
                    elif pos.side == Side.SELL and (pos.sl == 0 or pos.sl > be_level):
                        desired_sl = be_level
                        reason = f"Breakeven (+{be_r:.1f}R)"

            if trail_mult > 0 and atr_14 > 0:
                trail_dist = atr_14 * trail_mult
                cand_sl = (cur_price - trail_dist) if pos.side == Side.BUY else (cur_price + trail_dist)
                if pos.side == Side.BUY and cand_sl > pos.open_price and cand_sl > (desired_sl or pos.sl):
                    if cur_price - cand_sl >= min_stop_dist:
                        desired_sl = cand_sl
                        reason = f"ATR Trail ({trail_mult:.2f}x)"
                elif pos.side == Side.SELL and cand_sl < pos.open_price and (
                    pos.sl == 0 or cand_sl < (desired_sl or pos.sl)
                ):
                    if cand_sl - cur_price >= min_stop_dist:
                        desired_sl = cand_sl
                        reason = f"ATR Trail ({trail_mult:.2f}x)"

            if desired_sl is not None:
                rounded_sl = round(desired_sl, spec.digits)
                if pos.side == Side.BUY and rounded_sl > pos.sl + spec.point and rounded_sl <= cur_price - min_stop_dist:
                    mods.append(SLModification(ticket=pos.ticket, new_sl=rounded_sl, reason=reason))
                elif pos.side == Side.SELL and (
                    pos.sl == 0 or rounded_sl < pos.sl - spec.point
                ) and rounded_sl >= cur_price + min_stop_dist:
                    mods.append(SLModification(ticket=pos.ticket, new_sl=rounded_sl, reason=reason))

        return mods
