from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bot.models import (
    AccountInfo,
    BacktestMetrics,
    BotConfig,
    BotRunState,
    Candle,
    Position,
    Side,
    SymbolSpec,
    Tick,
    TradeRecord,
    TradeSignal,
)
from bot.risk_manager import RiskManager
from bot.wickless_strategy import WicklessStructureStrategy
from bot.structure_momentum_strategy import StructureMomentumStrategy


def default_xau_spec(symbol: str = "XAUUSDm") -> SymbolSpec:
    return SymbolSpec(
        symbol=symbol,
        point=0.01,
        digits=2,
        trade_tick_size=0.01,
        trade_tick_value=1.0,  # $1.00 per 0.01 move per 1.00 lot on standard XAUUSD (100 oz)
        trade_contract_size=100.0,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        stops_level=0,
        filling_mode=1,
    )


def load_candles_from_csv(csv_path: str | Path) -> list[Candle]:
    """
    Loads M1 OHLC candles from CSV. Supports standard headers:
      time/datetime/timestamp, open, high, low, close, [tick_volume|volume], [spread]
    """
    path = Path(csv_path)
    candles: list[Candle] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            norm = {k.strip().lower(): v.strip() for k, v in row.items() if k is not None and v is not None}
            t_raw = norm.get("time") or norm.get("datetime") or norm.get("timestamp") or norm.get("date")
            if not t_raw:
                continue
            if t_raw.isdigit():
                ts = datetime.fromtimestamp(int(t_raw), tz=timezone.utc)
            else:
                ts = datetime.fromisoformat(t_raw.replace("Z", "+00:00").replace(".", "-"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            vol_raw = norm.get("tick_volume") or norm.get("volume") or "0"
            spr_raw = norm.get("spread") or "0"
            candles.append(
                Candle(
                    time=ts,
                    open=float(norm["open"]),
                    high=float(norm["high"]),
                    low=float(norm["low"]),
                    close=float(norm["close"]),
                    tick_volume=int(float(vol_raw)),
                    spread=int(float(spr_raw)),
                )
            )
    candles.sort(key=lambda c: c.time)
    return candles


def generate_synthetic_xau_m1(num_bars: int = 1500, start_price: float = 3860.0, seed: int = 42) -> list[Candle]:
    """
    Generates realistic trending + pullback M1 Gold bars (including clean wickless candles)
    for offline testing and optimization verification when live MT5 history is unavailable.
    """
    import random
    from datetime import timedelta

    rng = random.Random(seed)
    candles: list[Candle] = []
    price = start_price
    t = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
    regime_dir = 1
    regime_len = 0

    for i in range(num_bars):
        regime_len += 1
        if regime_len > rng.randint(25, 65):
            regime_dir *= -1
            regime_len = 0

        drift = regime_dir * rng.uniform(0.05, 0.45)
        noise = rng.gauss(0, 0.65)
        o = round(price, 2)
        c = round(o + drift + noise, 2)

        # Periodically form clean wickless momentum candles in trend direction
        if i % 9 == 0:
            if regime_dir == 1:
                c = round(o + rng.uniform(0.6, 1.8), 2)
                l = o  # Zero lower wick
                h = round(c + rng.uniform(0.05, 0.35), 2)
            else:
                c = round(o - rng.uniform(0.6, 1.8), 2)
                h = o  # Zero upper wick
                l = round(c - rng.uniform(0.05, 0.35), 2)
        else:
            h = round(max(o, c) + rng.uniform(0.02, 0.55), 2)
            l = round(min(o, c) - rng.uniform(0.02, 0.55), 2)

        candles.append(
            Candle(
                time=t,
                open=o,
                high=h,
                low=l,
                close=c,
                tick_volume=rng.randint(40, 240),
                spread=25,
            )
        )
        price = c
        t += timedelta(minutes=1)
    return candles


@dataclass
class BacktestReport:
    metrics: BacktestMetrics
    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)


class EventBacktester:
    """
    Event-driven historical simulator sharing the exact WicklessStructureStrategy
    and RiskManager used in live MT5 execution—supporting intra-bar tick paths,
    spread/slippage, multi-entry scaling, and USD profit/loss auto-closing.
    """

    def __init__(
        self,
        cfg: BotConfig,
        spec: Optional[SymbolSpec] = None,
        initial_balance: float = 10000.0,
        default_spread_points: float = 25.0,
        slippage_points: float = 5.0,
        commission_per_lot_usd: float = 0.0,
    ) -> None:
        self.cfg = cfg
        self.spec = spec or default_xau_spec(cfg.mt5.symbol)
        self.initial_balance = initial_balance
        self.default_spread_points = default_spread_points
        self.slippage_points = slippage_points
        self.commission_per_lot_usd = commission_per_lot_usd

    def _calc_pnl_usd(self, pos: Position, exit_price: float) -> float:
        diff = (exit_price - pos.open_price) if pos.side == Side.BUY else (pos.open_price - exit_price)
        raw_usd = (diff / self.spec.trade_tick_size) * self.spec.trade_tick_value * pos.volume
        return raw_usd - abs(self.commission_per_lot_usd * pos.volume)

    def _synth_bar_ticks(self, bar: Candle) -> list[Tick]:
        spread_pts = bar.spread if bar.spread > 0 else self.default_spread_points
        spread_px = spread_pts * self.spec.point
        # Standard realistic intra-bar sequence: O -> L -> H -> C for bull, O -> H -> L -> C for bear
        if bar.close >= bar.open:
            path = [bar.open, bar.low, bar.high, bar.close]
        else:
            path = [bar.open, bar.high, bar.low, bar.close]
        return [
            Tick(time=bar.time, bid=px, ask=px + spread_px, last=px, volume=1.0)
            for px in path
        ]

    def run(self, candles: list[Candle], warmup_bars: int = 30) -> BacktestReport:
        if self.cfg.strategy.mode == "structure_momentum":
            strategy = StructureMomentumStrategy(
                cfg=self.cfg.strategy, symbol=self.spec.symbol, mintick=self.spec.point,
                sessions=self.cfg.sessions, max_spread_points=self.cfg.mt5.max_spread_points,
            )
        else:
            strategy = WicklessStructureStrategy(cfg=self.cfg.strategy, symbol=self.spec.symbol, mintick=self.spec.point)
        risk = RiskManager(self.cfg)

        balance = self.initial_balance
        peak_equity = self.initial_balance
        max_dd_usd = 0.0
        max_dd_pct = 0.0

        open_positions: list[Position] = []
        position_init_risk: dict[int, float] = {}
        position_tags: dict[int, str] = {}
        closed_trades: list[TradeRecord] = []
        equity_curve: list[float] = [self.initial_balance]

        next_ticket = 1
        current_day = candles[0].time.date() if candles else None
        daily_realized = 0.0
        run_state = BotRunState.RUNNING

        def close_pos(pos: Position, exit_price: float, close_time: datetime, reason: str) -> None:
            nonlocal balance, daily_realized
            pnl = self._calc_pnl_usd(pos, exit_price)
            balance += pnl
            daily_realized += pnl
            init_r = position_init_risk.pop(pos.ticket, 0.0)
            tag = position_tags.pop(pos.ticket, "Wickless")
            r_mult = (pnl / init_r) if init_r > 0 else (1.0 if pnl > 0 else -1.0)
            closed_trades.append(
                TradeRecord(
                    ticket=pos.ticket,
                    side=pos.side,
                    volume=pos.volume,
                    open_time=pos.open_time or close_time,
                    close_time=close_time,
                    open_price=pos.open_price,
                    close_price=exit_price,
                    sl=pos.sl,
                    tp=pos.tp,
                    pnl_usd=pnl,
                    r_multiple=r_mult,
                    exit_reason=reason,
                    tag=tag,
                )
            )
            risk.record_trade_close(close_time, pnl)

        def open_pos_from_signal(sig: TradeSignal, tick: Tick) -> None:
            nonlocal next_ticket
            floating = sum(p.net_pnl_usd for p in open_positions)
            acc = AccountInfo(
                balance=balance,
                equity=balance + floating,
                margin=0.0,
                margin_free=balance + floating,
            )
            allowed, lots, _ = risk.can_open_trade(
                signal=sig,
                open_positions=open_positions,
                account=acc,
                spec=self.spec,
                tick=tick,
                run_state=run_state,
            )
            if not allowed:
                return

            slip_px = self.slippage_points * self.spec.point
            fill_price = (tick.ask + slip_px) if sig.side == Side.BUY else (tick.bid - slip_px)
            # Re-check SL validity after spread/slippage
            if sig.side == Side.BUY and sig.sl_price >= fill_price:
                return
            if sig.side == Side.SELL and sig.sl_price <= fill_price:
                return

            sl_dist = abs(fill_price - sig.sl_price)
            init_risk_usd = (sl_dist / self.spec.trade_tick_size) * self.spec.trade_tick_value * lots
            ticket = next_ticket
            next_ticket += 1

            pos = Position(
                ticket=ticket,
                symbol=self.spec.symbol,
                side=sig.side,
                volume=lots,
                open_price=fill_price,
                sl=round(sig.sl_price, self.spec.digits),
                tp=round(sig.tp_price, self.spec.digits),
                profit_usd=0.0,
                magic=self.cfg.mt5.magic_number,
                open_time=tick.time,
                comment=sig.tag,
            )
            open_positions.append(pos)
            position_init_risk[ticket] = init_risk_usd
            position_tags[ticket] = sig.tag
            risk.record_trade_open(tick.time)

        def process_tick(tick: Tick, atr_val: float) -> None:
            nonlocal run_state
            # Update floating PnL on open positions
            for pos in open_positions:
                mark = tick.bid if pos.side == Side.BUY else tick.ask
                pos.profit_usd = self._calc_pnl_usd(pos, mark)

            # Check broker SL / TP hits (conservative: SL checked before TP if both hit)
            still_open: list[Position] = []
            for pos in open_positions:
                if pos.side == Side.BUY:
                    hit_sl = pos.sl > 0 and tick.bid <= pos.sl
                    hit_tp = pos.tp > 0 and tick.bid >= pos.tp
                else:
                    hit_sl = pos.sl > 0 and tick.ask >= pos.sl
                    hit_tp = pos.tp > 0 and tick.ask <= pos.tp

                if hit_sl:
                    close_pos(pos, pos.sl, tick.time, "SL")
                elif hit_tp:
                    close_pos(pos, pos.tp, tick.time, "TP")
                else:
                    still_open.append(pos)
            open_positions[:] = still_open

            # Refresh PnL & evaluate Dollar TP / Dollar SL / Daily Circuit Breakers
            for pos in open_positions:
                mark = tick.bid if pos.side == Side.BUY else tick.ask
                pos.profit_usd = self._calc_pnl_usd(pos, mark)

            exit_dec = risk.evaluate_dollar_exits(open_positions, daily_realized, tick.time)
            if exit_dec.close_all_reason:
                for pos in list(open_positions):
                    mark = tick.bid if pos.side == Side.BUY else tick.ask
                    close_pos(pos, mark, tick.time, exit_dec.close_all_reason.split(" ")[0])
                open_positions.clear()
                if exit_dec.halt_state is not None:
                    run_state = exit_dec.halt_state
            elif exit_dec.close_tickets:
                close_map = dict(exit_dec.close_tickets)
                rem: list[Position] = []
                for pos in open_positions:
                    if pos.ticket in close_map:
                        mark = tick.bid if pos.side == Side.BUY else tick.ask
                        close_pos(pos, mark, tick.time, close_map[pos.ticket].split(" ")[0])
                    else:
                        rem.append(pos)
                open_positions[:] = rem

            # Trailing & breakeven stops
            for mod in risk.evaluate_trailing_stops(open_positions, tick, atr_val, self.spec):
                for pos in open_positions:
                    if pos.ticket == mod.ticket:
                        pos.sl = mod.new_sl

            # Tick-level entry trigger (for retest_limit)
            for sig in strategy.on_tick(tick):
                open_pos_from_signal(sig, tick)

        for idx, bar in enumerate(candles):
            bar_day = bar.time.date()
            if current_day is not None and bar_day != current_day:
                current_day = bar_day
                daily_realized = 0.0
                if run_state in (BotRunState.HALTED_DAILY_PROFIT, BotRunState.HALTED_DAILY_LOSS):
                    run_state = BotRunState.RUNNING

            if idx < warmup_bars:
                strategy.on_bar_close(bar, emit_signals=False)
                continue

            atr_now = strategy.last_snapshot.atr_14 if strategy.last_snapshot else 0.0
            ticks = self._synth_bar_ticks(bar)
            for tk in ticks:
                process_tick(tk, atr_now)

            # Bar close evaluation
            _, bar_signals = strategy.on_bar_close(bar, emit_signals=True)
            close_tick = ticks[-1]
            for sig in bar_signals:
                open_pos_from_signal(sig, close_tick)

            # Update equity curve & drawdown
            floating = sum(
                self._calc_pnl_usd(p, close_tick.bid if p.side == Side.BUY else close_tick.ask)
                for p in open_positions
            )
            equity = balance + floating
            equity_curve.append(equity)
            if equity > peak_equity:
                peak_equity = equity
            dd = peak_equity - equity
            if dd > max_dd_usd:
                max_dd_usd = dd
            dd_pct = (dd / peak_equity * 100.0) if peak_equity > 0 else 0.0
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

        # Close remaining positions at end of backtest
        if candles and open_positions:
            last_bar = candles[-1]
            for pos in open_positions:
                close_pos(pos, last_bar.close, last_bar.time, "EOD")
            open_positions.clear()
            equity_curve.append(balance)

        metrics = self._compute_metrics(
            closed_trades=closed_trades,
            final_balance=balance,
            max_dd_usd=max_dd_usd,
            max_dd_pct=max_dd_pct,
            cancelled_setups=strategy.cancelled_setups_count,
        )
        return BacktestReport(metrics=metrics, trades=closed_trades, equity_curve=equity_curve)

    def _compute_metrics(
        self,
        closed_trades: list[TradeRecord],
        final_balance: float,
        max_dd_usd: float,
        max_dd_pct: float,
        cancelled_setups: int,
    ) -> BacktestMetrics:
        total = len(closed_trades)
        wins = sum(1 for t in closed_trades if t.pnl_usd > 0)
        losses = sum(1 for t in closed_trades if t.pnl_usd <= 0)
        win_rate = (wins / total * 100.0) if total > 0 else 0.0

        gross_profit = sum(t.pnl_usd for t in closed_trades if t.pnl_usd > 0)
        gross_loss = abs(sum(t.pnl_usd for t in closed_trades if t.pnl_usd < 0))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)

        net_profit = final_balance - self.initial_balance
        return_pct = (net_profit / self.initial_balance * 100.0) if self.initial_balance > 0 else 0.0
        expectancy = (net_profit / total) if total > 0 else 0.0

        total_r = sum(t.r_multiple for t in closed_trades)
        avg_r = (total_r / total) if total > 0 else 0.0

        # Trade-level Sharpe ratio
        if total > 1:
            pnls = [t.pnl_usd for t in closed_trades]
            mean_p = sum(pnls) / total
            var_p = sum((x - mean_p) ** 2 for x in pnls) / (total - 1)
            std_p = math.sqrt(var_p)
            sharpe = (mean_p / std_p) * math.sqrt(total) if std_p > 0 else 0.0
        else:
            sharpe = 0.0

        exit_breakdown: dict[str, int] = {}
        dollar_tp_closes = 0
        dollar_sl_closes = 0
        for t in closed_trades:
            exit_breakdown[t.exit_reason] = exit_breakdown.get(t.exit_reason, 0) + 1
            if "DOLLAR_TP" in t.exit_reason:
                dollar_tp_closes += 1
            if "DOLLAR_SL" in t.exit_reason:
                dollar_sl_closes += 1

        return BacktestMetrics(
            total_trades=total,
            wins=wins,
            losses=losses,
            win_rate_pct=round(win_rate, 2),
            net_profit_usd=round(net_profit, 2),
            return_pct=round(return_pct, 2),
            profit_factor=round(profit_factor, 3),
            expectancy_usd=round(expectancy, 2),
            max_drawdown_usd=round(max_dd_usd, 2),
            max_drawdown_pct=round(max_dd_pct, 2),
            sharpe_ratio=round(sharpe, 3),
            avg_r_multiple=round(avg_r, 3),
            total_r=round(total_r, 2),
            dollar_tp_closes=dollar_tp_closes,
            dollar_sl_closes=dollar_sl_closes,
            cancelled_setups=cancelled_setups,
            exit_breakdown=exit_breakdown,
        )
