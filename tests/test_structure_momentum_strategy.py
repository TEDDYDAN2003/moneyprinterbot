import unittest
from datetime import datetime, timezone

from bot.models import Candle, SessionConfig, Side, StrategyConfig, StructureSnapshot, Tick, PendingSetup
from bot.structure_momentum_strategy import StructureMomentumStrategy


class TestStructureMomentumStrategy(unittest.TestCase):
    def make_strategy(self, **kwargs):
        cfg = StrategyConfig(
            mode="structure_momentum",
            tick_confirm_enabled=True,
            tick_impulse_window=3,
            momentum_min_points=2,
            momentum_min_ratio=0.66,
            **kwargs,
        )
        return StructureMomentumStrategy(cfg, "XAUUSDm", 0.01)

    def test_bos_requires_confirmed_close_not_wick(self):
        self.assertFalse(StructureMomentumStrategy.is_confirmed_break(100.0, 100.0, Side.BUY))
        self.assertTrue(StructureMomentumStrategy.is_confirmed_break(100.01, 100.0, Side.BUY))
        self.assertFalse(StructureMomentumStrategy.is_confirmed_break(100.0, 100.0, Side.SELL))
        self.assertTrue(StructureMomentumStrategy.is_confirmed_break(99.99, 100.0, Side.SELL))

    def test_htf_alignment_requires_all_configured_timeframes(self):
        strategy = self.make_strategy()
        strategy.htf_snapshots[5] = StructureSnapshot(0, 1, 1)
        self.assertIsNone(strategy._aligned_side())
        strategy.htf_snapshots[15] = StructureSnapshot(0, 1, 1)
        self.assertEqual(strategy._aligned_side(), Side.BUY)
        strategy.htf_snapshots[15] = StructureSnapshot(0, -1, -1)
        self.assertIsNone(strategy._aligned_side())

    def test_tick_momentum_blocks_touch_until_direction_confirms(self):
        strategy = self.make_strategy()
        now = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        setup = PendingSetup("x", Side.BUY, 0, now, 99.0, 100.0, 98.0, None, 100.0, 102.0, "BOS", "retest_limit", 3)
        strategy.pending_setups = [setup]
        strategy.on_tick(Tick(now, 100.1, 100.2))
        strategy.on_tick(Tick(now, 100.0, 100.1))
        self.assertEqual(strategy.on_tick(Tick(now, 99.9, 100.0)), [])
        strategy.on_tick(Tick(now, 99.6, 99.7))
        strategy.on_tick(Tick(now, 99.7, 99.8))
        fired = strategy.on_tick(Tick(now, 99.8, 99.9))
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].side, Side.BUY)

    def test_filter_skips_session_spread_and_manual_news(self):
        cfg = StrategyConfig(mode="structure_momentum", manual_news_windows=["2026-11-16T10:00:00+00:00/2026-11-16T10:05:00+00:00"])
        strategy = StructureMomentumStrategy(
            cfg, "XAUUSDm", 0.01,
            sessions=SessionConfig(enabled=True, allowed_utc_hours=[(11, 12)]),
            max_spread_points=10,
        )
        candle = Candle(datetime(2026, 11, 16, 10, 2, tzinfo=timezone.utc), 100, 101, 99, 100.5, spread=20)
        snapshot = StructureSnapshot(0, 1, 1, atr_14=1)
        self.assertFalse(strategy._filters_ok(candle, snapshot))
        self.assertIn("outside_session", strategy.skip_reasons)
        strategy.sessions = SessionConfig(enabled=False)
        self.assertFalse(strategy._filters_ok(candle, snapshot))
        self.assertTrue(any("spread_too_wide" in x for x in strategy.skip_reasons))
        strategy.max_spread_points = 100
        self.assertFalse(strategy._filters_ok(candle, snapshot))
        self.assertIn("manual_news_window", strategy.skip_reasons)

    def test_fixed_distance_tp_and_sl(self):
        cfg = StrategyConfig(
            mode="structure_momentum",
            fixed_tp_distance=1.18,
            fixed_sl_distance=4.72,
            entry_mode="immediate",
            allow_m1_exploration=True,
            allow_m1_fallback=True,
            require_ma_slope=False,
            block_in_consolidation=False,
            rejection_required=False,
            tick_confirm_enabled=False,
            internal_len=2,
        )
        strategy = StructureMomentumStrategy(cfg, "XAUUSDm", 0.01)
        now = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        prices = [100.0, 102.0, 105.0, 103.0, 102.0, 106.0, 107.0]
        sigs = []
        for p in prices:
            c = Candle(now, p - 1.0, p + 1.0, p - 2.0, p)
            snap, bar_sigs = strategy.on_bar_close(c, emit_signals=True)
            if bar_sigs:
                sigs.extend(bar_sigs)
                break
        self.assertTrue(len(sigs) >= 1)
        sig = sigs[0]
        self.assertEqual(sig.side, Side.BUY)
        self.assertAlmostEqual(sig.tp_price, sig.entry_price + 1.18, places=3)
        self.assertAlmostEqual(sig.sl_price, sig.entry_price - 4.72, places=3)
        self.assertAlmostEqual(sig.rr, 1.18 / 4.72, places=3)


if __name__ == "__main__":
    unittest.main()
