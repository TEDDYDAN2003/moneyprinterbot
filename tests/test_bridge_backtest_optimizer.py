from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.backtester import EventBacktester, generate_synthetic_xau_m1
from bot.external_bridge import ExternalSignalBridge
from bot.models import BotConfig, ExternalBridgeConfig, Side, Tick
from bot.optimizer import StrategyOptimizer


class TestBridgeBacktestOptimizer(unittest.TestCase):
    def test_external_bridge_watches_entry_sl_tp_prices(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sig_file = Path(tmpdir) / "signals.json"
            payload = {
                "signals": [
                    {
                        "id": "ext1",
                        "symbol": "XAUUSDm",
                        "side": "BUY",
                        "entry_price": 3862.50,
                        "sl_price": 3860.00,
                        "tp_price": 3867.50,
                        "tolerance_points": 10,
                    }
                ]
            }
            sig_file.write_text(json.dumps(payload), encoding="utf-8")

            bridge = ExternalSignalBridge(
                cfg=ExternalBridgeConfig(signal_file=str(sig_file)),
                symbol="XAUUSDm",
                point=0.01,
            )

            now = datetime(2026, 11, 16, 11, 0, tzinfo=timezone.utc)
            # Market too high -> should wait
            t_high = Tick(time=now, bid=3864.00, ask=3864.20)
            self.assertEqual(len(bridge.evaluate_on_tick(t_high)), 0)

            # Market pulls back to 3862.45 -> triggers signal with configured SL/TP
            t_hit = Tick(time=now + timedelta(seconds=5), bid=3862.30, ask=3862.45)
            sigs = bridge.evaluate_on_tick(t_hit)
            self.assertEqual(len(sigs), 1)
            self.assertEqual(sigs[0].side, Side.BUY)
            self.assertAlmostEqual(sigs[0].sl_price, 3860.00, places=2)
            self.assertAlmostEqual(sigs[0].tp_price, 3867.50, places=2)

            # Consumed signal must not re-fire
            self.assertEqual(len(bridge.evaluate_on_tick(t_hit)), 0)

    def test_backtester_and_optimizer_end_to_end(self) -> None:
        candles = generate_synthetic_xau_m1(num_bars=600, seed=7)
        cfg = BotConfig()
        cfg.dollar_exits.trade_tp_usd = 15.0
        cfg.dollar_exits.basket_tp_usd = 15.0

        bt = EventBacktester(cfg=cfg, initial_balance=10000.0)
        report = bt.run(candles)
        self.assertGreater(report.metrics.total_trades, 0)
        self.assertGreater(len(report.equity_curve), 100)

        opt = StrategyOptimizer(base_cfg=cfg, initial_balance=10000.0, min_trades=3)
        summary = opt.optimize(candles, n_trials=6, prefer_optuna=False)
        self.assertIsNotNone(summary.best_trial)
        self.assertGreaterEqual(len(summary.top_trials), 1)


if __name__ == "__main__":
    unittest.main()
