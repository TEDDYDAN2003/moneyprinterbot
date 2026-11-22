"""Optional audit evidence is never a fill or an upgrade to a historical tag."""

import json
import tempfile
import unittest
from pathlib import Path

from fixtures import MAGIC, FakeMT, T, deal
from ledger import db_open, get, identity, make_views, sync


class ReceiptEvidenceTests(unittest.TestCase):
    def test_only_exact_consistent_receipt_enriches_confirmed_ticket(self):
        tag = f"h45_{T}_1"
        ds = [
            deal(1, 2, T + 1, 1, 0, 0.1, 101, comment=tag),
            deal(3, 2, T + 12, 0, 1, 0.1, 100, profit=1),
        ]
        mt = FakeMT(ds, T + 120)
        req = {
            "type": 1,
            "magic": MAGIC,
            "symbol": "XAUUSDm",
            "comment": tag,
            "sl": 103.5,
            "tp": 100,
        }
        base = {
            "event": "demo_result",
            "retcode": 10009,
            "deal": 1,
            "leg": 1,
            "tf": 45,
            "symbol": "XAUUSDm",
            "signal": T,
            "entry_timing": "confirmed_close",
            "trigger_msc": T * 1000,
            "entry_side": -1,
            "cross_candle_open_s": T - 2700,
            "initial_stop_policy": "confirmed_cross_candle_wick_v1",
            "cross_candle_wick": 103.4,
            "frozen_cross_sl": 103.5,
            "request": req,
            "send_tick_time_msc": T * 1000 + 200,
            "send_bid": 101,
            "send_ask": 101.1,
        }
        cases = [
            ({}, True),
            ({"event": "batch_preview"}, False),
            ({"initial_stop_policy": None}, False),
            ({"scanner_magic": 123}, False),
            ({"deal": 0}, False),
            ({"retcode": 10006}, False),
            ({"deal": 999}, False),
            ({"entry_side": 1}, False),
            ({"entry_timing": "live_exit_only"}, False),
            ({"signal": T + 60}, False),
            ({"frozen_cross_sl": 103.6}, False),
            ({"cross_candle_open_s": T - 900}, False),
            ({"send_tick_time_msc": T * 1000 - 1}, False),
            ({"send_bid": 102}, False),
            ({"request": req | {"comment": f"h15_{T}_1"}}, False),
        ]
        for edits, verified in cases:
            with self.subTest(edits=edits), tempfile.TemporaryDirectory() as root:
                db = db_open(":memory:")
                try:
                    log = (
                        Path(root)
                        / "reports/hull_runtime"
                        / identity(mt, MAGIC)["key"]
                        / "demo.jsonl"
                    )
                    log.parent.mkdir(parents=True)
                    line = json.dumps(base | edits)
                    log.write_text(line)  # partial append isn't consumed
                    sync(db, mt, mt.at, repo=root)
                    self.assertEqual(get(db, "log_cursor")["off"], 0)
                    with log.open("a") as f:
                        f.write("\n")
                    sync(db, mt, mt.at + 1, repo=root)
                    rows, _, _ = make_views(db)
                    self.assertEqual(len(rows["tickets"]), 1)
                    self.assertEqual(rows["tickets"][0]["net_profit"], "1.00")
                    self.assertEqual(
                        bool(rows["tickets"][0]["frozen_cross_sl"]), verified
                    )
                    self.assertEqual(
                        bool(rows["tickets"][0]["send_quote_utc"]), verified
                    )
                    if verified:
                        # A repeated write of the identical receipt is harmless. A
                        # different plausible wick on that exact deal is not evidence.
                        for offset, (extra, expected) in enumerate(
                            (
                                (base, True),
                                (base | {"cross_candle_wick": 103.3}, False),
                                (base, False),
                            ),
                            2,
                        ):
                            with log.open("a") as f:
                                f.write(json.dumps(extra) + "\n")
                            sync(db, mt, mt.at + offset, repo=root)
                            self.assertEqual(
                                bool(
                                    make_views(db)[0]["tickets"][0]["frozen_cross_sl"]
                                ),
                                expected,
                            )
                finally:
                    db.close()
