from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from bot.models import Candle, Side, StrategyConfig, Tick
from bot.wickless_strategy import WicklessStructureStrategy


def make_candle(
    idx: int,
    o: float,
    h: float,
    l: float,
    c: float,
) -> Candle:
    t = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=idx)
    return Candle(time=t, open=o, high=h, low=l, close=c, tick_volume=100, spread=20)


class TestWicklessStructureStrategy(unittest.TestCase):
    def test_bullish_wickless_no_structure_retest_limit_tick_trigger(self) -> None:
        cfg = StrategyConfig(
            use_structure=False,
            entry_mode="retest_limit",
            zone_pips=3.0,
            pip_size=0.1,  # zoneBuf = 0.30
            rr_value=1.5,
            retest_bars=5,
        )
        strat = WicklessStructureStrategy(cfg=cfg, symbol="XAUUSDm", mintick=0.01)

        # Bullish wickless candle: open == low (zero lower wick)
        c0 = make_candle(0, 3860.00, 3862.00, 3860.00, 3861.80)
        _, sigs = strat.on_bar_close(c0)
        self.assertEqual(len(sigs), 0)
        self.assertEqual(len(strat.pending_setups), 1)
        setup = strat.pending_setups[0]
        self.assertEqual(setup.side, Side.BUY)
        self.assertAlmostEqual(setup.entry_price or 0.0, 3860.30, places=2)
        self.assertAlmostEqual(setup.sl_price, 3860.00, places=2)
        # risk = 0.30, RR 1.5 -> TP = 3860.75
        self.assertAlmostEqual(setup.tp_price or 0.0, 3860.75, places=2)

        # Tick above zone does not trigger
        t1 = Tick(time=c0.time + timedelta(seconds=10), bid=3860.90, ask=3861.10)
        self.assertEqual(len(strat.on_tick(t1)), 0)

        # Tick pulls back into zone edge (ask <= 3860.30) -> triggers BUY!
        t2 = Tick(time=c0.time + timedelta(seconds=20), bid=3860.10, ask=3860.28)
        fired = strat.on_tick(t2)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].side, Side.BUY)
        self.assertAlmostEqual(fired[0].entry_price, 3860.28, places=2)
        self.assertEqual(len(strat.pending_setups), 0)

    def test_immediate_entry_mode_fires_on_close(self) -> None:
        cfg = StrategyConfig(
            use_structure=False,
            entry_mode="immediate",
            rr_value=2.0,
        )
        strat = WicklessStructureStrategy(cfg=cfg, symbol="XAUUSDm", mintick=0.01)

        # Bearish wickless candle: open == high
        c0 = make_candle(0, 3865.00, 3865.00, 3863.00, 3863.20)
        _, sigs = strat.on_bar_close(c0)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].side, Side.SELL)
        self.assertAlmostEqual(sigs[0].entry_price, 3863.20, places=2)
        self.assertAlmostEqual(sigs[0].sl_price, 3865.00, places=2)
        # risk = 1.80, RR 2.0 -> TP = 3859.60
        self.assertAlmostEqual(sigs[0].tp_price, 3859.60, places=2)

    def test_retest_rejection_mode_and_expiration(self) -> None:
        cfg = StrategyConfig(
            use_structure=False,
            entry_mode="retest_rejection",
            zone_pips=3.0,
            pip_size=0.1,
            need_reject=True,
            retest_bars=2,
        )
        strat = WicklessStructureStrategy(cfg=cfg, symbol="XAUUSDm", mintick=0.01)

        # Bar 0: bullish wickless (zone: [3860.00, 3860.30])
        c0 = make_candle(0, 3860.00, 3861.50, 3860.00, 3861.40)
        strat.on_bar_close(c0)
        self.assertEqual(len(strat.pending_setups), 1)

        # Bar 1: touches zone (low=3860.20) and rejects bullishly (close > open)
        c1 = make_candle(1, 3861.00, 3861.60, 3860.20, 3861.45)
        _, sigs1 = strat.on_bar_close(c1)
        self.assertEqual(len(sigs1), 1)
        self.assertEqual(sigs1[0].side, Side.BUY)

        # Now test expiration after retest_bars=2 without touch
        c2 = make_candle(2, 3862.00, 3863.50, 3862.00, 3863.40)
        strat.on_bar_close(c2)
        self.assertEqual(len(strat.pending_setups), 1)

        c3 = make_candle(3, 3863.40, 3864.20, 3863.10, 3864.00)  # age 1, no touch
        strat.on_bar_close(c3)
        self.assertEqual(len(strat.pending_setups), 1)

        c4 = make_candle(4, 3864.00, 3864.90, 3863.80, 3864.70)  # age 2 == retest_bars -> expires
        strat.on_bar_close(c4)
        self.assertEqual(len(strat.pending_setups), 0)
        self.assertGreaterEqual(strat.cancelled_setups_count, 1)

    def test_market_structure_bos_and_mss_flip(self) -> None:
        cfg = StrategyConfig(
            use_structure=True,
            internal_len=1,
            external_len=3,
            entry_mode="immediate",
            rr_value=1.0,
        )
        strat = WicklessStructureStrategy(cfg=cfg, symbol="XAUUSDm", mintick=0.01)

        # Build swing high at bar 1 (3862), swing low at bar 3 (3859), then close > 3862 -> BOS UP
        bars = [
            make_candle(0, 3860.0, 3861.0, 3859.8, 3860.8),
            make_candle(1, 3860.8, 3862.5, 3860.5, 3861.2),  # pivot high at bar 1 (confirmed at bar 2)
            make_candle(2, 3861.2, 3861.4, 3860.0, 3860.2),
            make_candle(3, 3860.2, 3860.4, 3859.0, 3859.5),  # pivot low at bar 3 (confirmed at bar 4)
            make_candle(4, 3859.5, 3861.8, 3859.4, 3861.6),
            make_candle(5, 3861.6, 3863.2, 3861.5, 3863.0),  # close > 3862.5 => BOS UP
            make_candle(6, 3863.0, 3863.1, 3858.0, 3858.2),  # close < prot_low => MSS DOWN
        ]
        snaps = [strat.on_bar_close(b, emit_signals=False)[0] for b in bars]
        self.assertEqual(snaps[5].internal_trend, 1)
        self.assertIn("INT BOS UP", snaps[5].event_note or "")
        self.assertEqual(snaps[6].internal_trend, -1)
        self.assertIn("INT MSS DOWN", snaps[6].event_note or "")


if __name__ == "__main__":
    unittest.main()
