from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Optional

from bot.backtester import default_xau_spec
from bot.broker_interface import BrokerAdapter
from bot.engine import LiveTradingEngine
from bot.models import (
    AccountInfo,
    BotConfig,
    BotRunState,
    Candle,
    Position,
    Side,
    SymbolSpec,
    Tick,
    TradeSignal,
)
from bot.risk_manager import RiskManager


class MockBroker(BrokerAdapter):
    def __init__(self) -> None:
        self.spec = default_xau_spec("XAUUSDm")
        self.account = AccountInfo(balance=10000.0, equity=10000.0, margin=0.0, margin_free=10000.0)
        self.tick = Tick(time=datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc), bid=3860.00, ask=3860.20)
        self.candles: list[Candle] = []
        self.positions: list[Position] = []
        self.closed_tickets: list[int] = []
        self.next_ticket = 100

    def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        pass

    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        return self.spec

    def get_account_info(self) -> AccountInfo:
        return self.account

    def get_latest_tick(self, symbol: str) -> Optional[Tick]:
        return self.tick

    def get_ticks_from(self, symbol: str, from_time: datetime, count: int) -> list[Tick]:
        return [self.tick]

    def get_m1_candles(self, symbol: str, count: int) -> list[Candle]:
        return self.candles[-count:]

    def get_open_positions(self, symbol: str, magic: Optional[int] = None) -> list[Position]:
        return [p for p in self.positions if magic is None or p.magic == magic]

    def open_market_order(
        self, symbol: str, side: Side, volume: float, sl: float, tp: float, magic: int, deviation: int, comment: str
    ) -> Optional[int]:
        t = self.next_ticket
        self.next_ticket += 1
        px = self.tick.ask if side == Side.BUY else self.tick.bid
        self.positions.append(
            Position(
                ticket=t,
                symbol=symbol,
                side=side,
                volume=volume,
                open_price=px,
                sl=sl,
                tp=tp,
                profit_usd=0.0,
                magic=magic,
                open_time=self.tick.time,
                comment=comment,
            )
        )
        return t

    def modify_position_sl_tp(self, ticket: int, symbol: str, sl: float, tp: float) -> bool:
        for p in self.positions:
            if p.ticket == ticket:
                p.sl = sl
                p.tp = tp
                return True
        return False

    def close_position(self, pos: Position, deviation: int, comment: str) -> bool:
        self.closed_tickets.append(pos.ticket)
        self.positions = [p for p in self.positions if p.ticket != pos.ticket]
        return True


class TestRiskAndDollarExits(unittest.TestCase):
    def test_loss_controls_block_new_entries(self) -> None:
        cfg = BotConfig()
        cfg.risk.max_trades_per_day = 1
        cfg.risk.max_consecutive_losses = 2
        cfg.risk.loss_cooldown_minutes = 60
        rm = RiskManager(cfg)
        spec = default_xau_spec("XAUUSDm")
        account = AccountInfo(balance=1000.0, equity=1000.0, margin=0.0, margin_free=1000.0)
        now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
        signal = TradeSignal(
            signal_id="loss-control",
            side=Side.BUY,
            entry_price=3860.0,
            sl_price=3859.0,
            tp_price=3862.0,
            rr=2.0,
            tag="test",
            source="test",
            created_time=now,
        )
        tick = Tick(time=now, bid=3859.9, ask=3860.1)

        rm.record_trade_open(now)
        allowed, _, reason = rm.can_open_trade(
            signal, [], account, spec, tick, BotRunState.RUNNING
        )
        self.assertFalse(allowed)
        self.assertIn("Daily trade cap", reason)

        cfg.risk.max_trades_per_day = 0
        rm.record_trade_close(now, -5.0)
        rm.record_trade_close(now, -5.0)
        later = Tick(time=now + timedelta(minutes=30), bid=3859.9, ask=3860.1)
        allowed, _, reason = rm.can_open_trade(
            signal, [], account, spec, later, BotRunState.RUNNING
        )
        self.assertFalse(allowed)
        self.assertIn("Consecutive-loss cooldown", reason)

    def test_profit_streak_increases_lot_with_risk_cap(self) -> None:
        cfg = BotConfig()
        cfg.risk.sizing_mode = "fixed_lot"
        cfg.risk.fixed_lot = 0.01
        cfg.risk.profit_streak_scaling_enabled = True
        cfg.risk.profit_streak_step_wins = 2
        cfg.risk.profit_streak_add_lots = 0.01
        cfg.risk.profit_streak_max_lot = 0.05
        rm = RiskManager(cfg)
        spec = default_xau_spec("XAUUSDm")
        account = AccountInfo(balance=10000.0, equity=10000.0, margin=0.0, margin_free=10000.0)
        now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
        signal = TradeSignal(
            signal_id="streak",
            side=Side.BUY,
            entry_price=3860.0,
            sl_price=3859.0,
            tp_price=3862.0,
            rr=2.0,
            tag="test",
            source="test",
            created_time=now,
        )
        tick = Tick(time=now, bid=3859.9, ask=3860.1)

        rm.record_trade_close(now, 1.0)
        rm.record_trade_close(now, 1.0)
        allowed, lots, _ = rm.can_open_trade(
            signal, [], account, spec, tick, BotRunState.RUNNING
        )
        self.assertTrue(allowed)
        self.assertEqual(lots, 0.02)

        rm.record_trade_close(now, -1.0)
        allowed, lots, _ = rm.can_open_trade(
            signal, [], account, spec, tick, BotRunState.RUNNING
        )
        self.assertTrue(allowed)
        self.assertEqual(lots, 0.01)

    def test_profit_streak_expands_position_ceiling(self) -> None:
        cfg = BotConfig()
        cfg.risk.allow_scaling = True
        cfg.risk.max_open_positions = 10
        cfg.risk.profit_streak_scaling_enabled = True
        cfg.risk.profit_streak_positions_start = 3
        cfg.risk.profit_streak_positions_step_wins = 2
        cfg.risk.profit_streak_positions_add = 1
        cfg.risk.profit_streak_positions_max = 10
        rm = RiskManager(cfg)
        spec = default_xau_spec("XAUUSDm")
        account = AccountInfo(balance=10000.0, equity=10000.0, margin=0.0, margin_free=10000.0)
        now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
        signal = TradeSignal(
            signal_id="position-ceiling",
            side=Side.BUY,
            entry_price=3860.0,
            sl_price=3859.0,
            tp_price=3862.0,
            rr=2.0,
            tag="test",
            source="test",
            created_time=now,
        )
        tick = Tick(time=now, bid=3859.9, ask=3860.1)
        positions = [
            Position(
                ticket=i, symbol="XAUUSDm", side=Side.BUY, volume=0.01,
                open_price=3859.0, sl=3858.0, tp=3862.0, profit_usd=1.0,
            )
            for i in range(1, 4)
        ]

        allowed, _, reason = rm.can_open_trade(
            signal, positions, account, spec, tick, BotRunState.RUNNING
        )
        self.assertFalse(allowed)
        self.assertIn("Max open positions reached (3)", reason)

        rm.record_trade_close(now, 1.0)
        rm.record_trade_close(now, 1.0)
        allowed, _, reason = rm.can_open_trade(
            signal, positions, account, spec, tick, BotRunState.RUNNING
        )
        self.assertTrue(allowed, reason)

    def test_dollar_tp_15_and_basket_sl_auto_close(self) -> None:
        cfg = BotConfig()
        cfg.dollar_exits.trade_tp_usd = 15.0
        cfg.dollar_exits.basket_tp_usd = 0.0
        cfg.dollar_exits.basket_sl_usd = 30.0
        rm = RiskManager(cfg)

        p1 = Position(ticket=1, symbol="XAUUSDm", side=Side.BUY, volume=0.10, open_price=3860.0, sl=3858.0, tp=3865.0, profit_usd=15.50)
        p2 = Position(ticket=2, symbol="XAUUSDm", side=Side.BUY, volume=0.10, open_price=3861.0, sl=3859.0, tp=3866.0, profit_usd=4.00)

        dec = rm.evaluate_dollar_exits([p1, p2], daily_realized_pnl_usd=0.0)
        self.assertIsNone(dec.close_all_reason)
        self.assertEqual(len(dec.close_tickets), 1)
        self.assertEqual(dec.close_tickets[0][0], 1)

        # Now test basket SL hit
        p1.profit_usd = -18.0
        p2.profit_usd = -14.0
        dec2 = rm.evaluate_dollar_exits([p1, p2], daily_realized_pnl_usd=0.0)
        self.assertIsNotNone(dec2.close_all_reason)
        self.assertIn("BASKET_DOLLAR_SL", dec2.close_all_reason or "")

    def test_lot_scaled_dollar_target(self) -> None:
        cfg = BotConfig()
        cfg.dollar_exits.trade_tp_usd = 0.0
        cfg.dollar_exits.trade_tp_usd_per_lot = 100.0
        cfg.dollar_exits.basket_tp_usd = 0.0
        rm = RiskManager(cfg)

        small = Position(
            ticket=1, symbol="XAUUSDm", side=Side.BUY, volume=0.01,
            open_price=3860.0, sl=3858.0, tp=3865.0, profit_usd=1.01,
        )
        large = Position(
            ticket=2, symbol="XAUUSDm", side=Side.BUY, volume=0.05,
            open_price=3860.0, sl=3858.0, tp=3865.0, profit_usd=4.99,
        )

        dec = rm.evaluate_dollar_exits([small, large], daily_realized_pnl_usd=0.0)
        self.assertEqual([ticket for ticket, _ in dec.close_tickets], [1])

    def test_stagnation_exit_closes_repeated_breakeven_flips(self) -> None:
        cfg = BotConfig()
        cfg.dollar_exits.stagnation_enabled = True
        cfg.dollar_exits.stagnation_min_minutes = 15
        cfg.dollar_exits.stagnation_pnl_band_usd = 0.25
        cfg.dollar_exits.stagnation_flip_count = 3
        rm = RiskManager(cfg)
        opened = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
        position = Position(
            ticket=7, symbol="XAUUSDm", side=Side.BUY, volume=0.01,
            open_price=3860.0, sl=3858.0, tp=3865.0, profit_usd=0.10,
            open_time=opened,
        )
        for pnl in (0.10, -0.10, 0.10, -0.10, 0.10, -0.10, 0.05):
            position.profit_usd = pnl
            decision = rm.evaluate_dollar_exits(
                [position], daily_realized_pnl_usd=0.0,
                now_utc=opened + timedelta(minutes=20),
            )
        self.assertEqual([ticket for ticket, _ in decision.close_tickets], [7])
        self.assertIn("STAGNATION_EXIT", decision.close_tickets[0][1])

    def test_multi_entry_scaling_caps_and_no_martingale(self) -> None:
        cfg = BotConfig()
        cfg.risk.allow_scaling = True
        cfg.risk.max_open_positions = 2
        cfg.risk.scale_only_in_profit = True
        cfg.risk.scale_min_profit_r = 0.30
        rm = RiskManager(cfg)

        spec = default_xau_spec("XAUUSDm")
        acc = AccountInfo(balance=10000.0, equity=10000.0, margin=0.0, margin_free=10000.0)
        sig = TradeSignal(
            signal_id="s1",
            side=Side.BUY,
            entry_price=3862.0,
            sl_price=3860.0,
            tp_price=3864.0,
            rr=1.0,
            tag="Trend",
            source="wickless",
            created_time=datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc),
        )

        # Existing trade currently in loss -> scaling must be blocked (never average down)
        losing_pos = Position(ticket=1, symbol="XAUUSDm", side=Side.BUY, volume=0.50, open_price=3862.0, sl=3860.0, tp=3864.0, profit_usd=-10.0)
        tick_down = Tick(time=sig.created_time, bid=3861.50, ask=3861.70)
        ok, _, reason = rm.can_open_trade(sig, [losing_pos], acc, spec, tick_down, BotRunState.RUNNING)
        self.assertFalse(ok)
        self.assertIn("Scale blocked", reason)

        # Existing trade at +0.5R -> scaling allowed!
        tick_up = Tick(time=sig.created_time, bid=3863.00, ask=3863.20)
        ok2, lots2, _ = rm.can_open_trade(sig, [losing_pos], acc, spec, tick_up, BotRunState.RUNNING)
        self.assertTrue(ok2)
        self.assertGreater(lots2, 0.0)

        # At max_open_positions = 2 -> 3rd entry blocked
        pos2 = Position(ticket=2, symbol="XAUUSDm", side=Side.BUY, volume=0.50, open_price=3863.2, sl=3861.0, tp=3865.0, profit_usd=5.0)
        ok3, _, reason3 = rm.can_open_trade(sig, [losing_pos, pos2], acc, spec, tick_up, BotRunState.RUNNING)
        self.assertFalse(ok3)
        self.assertIn("Max open positions", reason3)

    def test_live_engine_closes_at_15_dollar_profit(self) -> None:
        cfg = BotConfig()
        cfg.strategy.use_structure = False
        cfg.strategy.entry_mode = "immediate"
        cfg.dollar_exits.trade_tp_usd = 15.0
        cfg.dollar_exits.basket_tp_usd = 15.0

        broker = MockBroker()
        base_t = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        # Historical bars + forming bar
        broker.candles = [
            Candle(time=base_t + timedelta(minutes=i), open=3858.0, high=3859.0, low=3857.5, close=3858.5)
            for i in range(10)
        ]

        engine = LiveTradingEngine(cfg=cfg, broker=broker)
        engine.initialize()
        engine.set_running()

        # New closed bullish wickless bar + forming bar
        broker.candles.append(
            Candle(time=base_t + timedelta(minutes=10), open=3860.00, high=3862.00, low=3860.00, close=3861.80)
        )
        broker.candles.append(
            Candle(time=base_t + timedelta(minutes=11), open=3861.80, high=3862.10, low=3861.70, close=3861.90)
        )
        broker.tick = Tick(time=base_t + timedelta(minutes=11), bid=3861.80, ask=3862.00)

        engine.step_once()
        self.assertEqual(len(broker.positions), 1)

        # Simulate price moving up so floating profit hits +$16.00 (>= $15 target)
        broker.positions[0].profit_usd = 16.00
        engine.step_once()
        self.assertEqual(len(broker.positions), 0)
        self.assertEqual(len(broker.closed_tickets), 1)

    def test_auto_calculated_tp_sl_from_target_usd(self):
        cfg = BotConfig()
        cfg.strategy.mode = "structure_momentum"
        cfg.strategy.pace_mode = "slow"
        cfg.strategy.m1_fallback_max_lot = 0.05
        cfg.strategy.target_tp_usd = 1.00
        cfg.strategy.target_sl_usd = 6.00
        cfg.risk.sizing_mode = "fixed_lot"
        cfg.risk.fixed_lot = 0.05
        cfg.risk.min_lot = 0.01
        cfg.risk.max_lot_per_trade = 0.05
        cfg.sessions.enabled = False

        broker = MockBroker()
        engine = LiveTradingEngine(cfg=cfg, broker=broker)
        engine.initialize()
        engine.set_running()

        tick = Tick(time=datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc), bid=3860.00, ask=3860.20)
        sig = TradeSignal("sig1", Side.BUY, 3860.20, 3855.00, 3865.00, 1.0, "M1_EXPLORATION", "structure_momentum", tick.time)
        engine._execute_signal(sig, tick)

        self.assertEqual(len(broker.positions), 1)
        pos = broker.positions[0]
        self.assertEqual(pos.volume, 0.05)
        # 0.05 lot XAUUSDm: 1 point = $5.00
        # TP dist = 1.00 / 5.00 + spread (0.20) = 0.40 -> tp = 3860.20 + 0.40 = 3860.60
        # SL dist = 6.00 / 5.00 = 1.20 -> sl = 3860.20 - 1.20 = 3859.00
        self.assertAlmostEqual(pos.tp, 3860.60, places=2)
        self.assertAlmostEqual(pos.sl, 3859.00, places=2)


if __name__ == "__main__":
    unittest.main()
