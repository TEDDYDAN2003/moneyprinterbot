"""Price-side evidence on exact deals, never inferred from a chart or a changed P/L."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fixtures import MAGIC, FakeMT, T, deal
from ledger import db_open, get, import_log, index_retest, make_views, sync, tag

V1 = "sell_ask_stop_with_live_spread_buffer_v1"
V2 = "sell_ask_entry_only_exact_risk_v2"


def market_receipt(child=4, did=1):
    return {
        "event": "demo_result",
        "symbol": "XAUUSDm",
        "tf": 5,
        "leg": 1,
        "signal": T,
        "initial_stop_policy": "confirmed_pair_trigger_candle_wick_v1",
        "entry_policy": "closed_pair_structure_cross_v1",
        "entry_side": -1,
        "entry_timing": "confirmed_close",
        "parent_tf": 5,
        "trigger_tf": child,
        "trigger_source": "low",
        "trigger_msc": T * 1000,
        "trigger_structure_close_s": T,
        "parent_structure_close_s": T,
        "parent_structure_bias": -1,
        "trigger_structure_bias": -1,
        "cross_candle_open_s": T - child * 60,
        "cross_candle_wick": 109,
        "frozen_cross_sl": 110,
        "raw_chart_sl": 110,
        "send_tick_time_msc": T * 1000 + 200,
        "close_to_submit_ms": 300,
        "max_entry_age_s": 8,
        "max_quote_age_s": 4,
        "stop_tick_size": 1,
        "send_bid": 100,
        "send_ask": 102,
        "stop_reference_bid": 100,
        "stop_reference_ask": 102,
        "stop_spread_policy": V2,
        "stop_spread_buffer_price": 2,
        "planned_stop_spread_quote_price": 2,
        "stop_spread_quote_price": 2,
        "retcode": 10009,
        "deal": did,
        "request": {
            "type": 1,
            "price": 100,
            "sl": 112,
            "tp": 90,
            "magic": MAGIC,
            "symbol": "XAUUSDm",
            "comment": f"p5x{child}_{T}_1",
        },
    }


def ingest(db, log, ev):
    log.write_text(json.dumps(ev) + "\n")
    import_log(db, log, MAGIC)


class StopReceiptTests(unittest.TestCase):
    def test_market_evidence_needs_consistent_policy_grid_quotes_and_request(self):
        e = market_receipt()
        req = e["request"]
        with tempfile.TemporaryDirectory() as tmp:
            for edits in (
                {},
                {"request": req | {"sl": 113}},
                {"request": req | {"price": 99}},
                {"stop_spread_policy": "unknown"},
                {"stop_spread_policy": V1},
                {"raw_chart_sl": 111},
                {"stop_reference_ask": 101},
                {"send_ask": 103},
                {"planned_stop_spread_quote_price": 3},
                {"stop_spread_buffer_price": 1},
                {"cross_candle_wick": 109.9},
                {"stop_tick_size": 0},
                {"trigger_tf": 3},
            ):
                with self.subTest(edits=edits), closing(db_open(":memory:")) as c:
                    ingest(c, Path(tmp) / "demo.jsonl", e | edits)
                    # Raw broker request and optional proof are intentionally separate indices.
                    proof = c.execute("SELECT wick,sl FROM cross_receipts").fetchall()
                    self.assertEqual(proof, [] if edits else [("109", "110")])
                    self.assertEqual(get(c, "log_cursor")["bad"], int(bool(edits)))

    def test_legacy_receipts_keep_their_exact_entry_math(self):
        e = market_receipt(child=3)
        for k in ("raw_chart_sl", "stop_reference_bid", "stop_reference_ask"):
            del e[k]
        e.update(stop_spread_policy=V1)
        with tempfile.TemporaryDirectory() as tmp:
            for version in (1, 0):
                if version == 0:
                    for k in (
                        "stop_spread_policy",
                        "stop_spread_buffer_price",
                        "planned_stop_spread_quote_price",
                        "stop_spread_quote_price",
                    ):
                        del e[k]
                    e["request"]["sl"] = 110
                with self.subTest(version=version), closing(db_open(":memory:")) as c:
                    ingest(c, Path(tmp) / "demo.jsonl", e)
                    self.assertEqual(
                        c.execute("SELECT sl FROM receipts").fetchone(),
                        ("112" if version else "110",),
                    )
                    self.assertEqual(
                        c.execute("SELECT sl FROM cross_receipts").fetchone(), ("110",)
                    )

    def test_conflicting_child_is_not_a_fill_or_a_shared_setup(self):
        old, new = market_receipt(3, 1), market_receipt(4, 3)
        ds = []
        for e in (old, new):
            self.assertEqual(tag(e["request"]["comment"]), (5, T, 1))
            i = e["deal"]
            ds += [
                deal(i, i, T + 1, 1, 0, 0.1, 100, comment=e["request"]["comment"]),
                deal(i + 1, i, T + 9, 0, 1, 0.1, 98, profit=2, magic=0),
            ]
        mt = FakeMT(ds, T + 180)
        with tempfile.TemporaryDirectory() as tmp, closing(db_open(":memory:")) as c:
            log = Path(tmp) / "demo.jsonl"
            log.write_text("\n".join(map(json.dumps, (old, new))) + "\n")
            import_log(c, log, MAGIC)
            sync(c, mt, mt.at)
            data = make_views(c)[0]
            self.assertEqual(len(data["tickets"]), 2)
            self.assertEqual(len(data["setups"]), 2)
            self.assertEqual(
                {
                    (r["trigger_timeframe"], r["ticket_count"], r["net_profit"])
                    for r in data["setups"]
                },
                {("M3", "1", "2.00"), ("M4", "1", "2.00")},
            )
            self.assertTrue(
                all(
                    t["frozen_cross_sl"] == "110" and t["initial_sl"] == "112"
                    for t in data["tickets"]
                )
            )
        # A receipt and a broker tag that disagree on the child do not identify a lane.
        ds[0].comment = new["request"]["comment"]
        with tempfile.TemporaryDirectory() as tmp, closing(db_open(":memory:")) as c:
            ingest(c, Path(tmp) / "demo.jsonl", old)
            sync(c, mt, mt.at)
            t = next(r for r in make_views(c)[0]["tickets"] if r["position_id"] == "1")
            self.assertEqual(
                (t["setup_id"], t["frozen_cross_sl"], t["net_profit"]), ("", "", "2.00")
            )

    def test_v7_indices_upgrade_once_without_relabelling_history_or_settling_other_child(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.sqlite3"
            with closing(sqlite3.connect(path)) as c:
                c.executescript("""CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT NOT NULL);
                  INSERT INTO meta VALUES('until','777');
                  CREATE TABLE retest_settled(symbol TEXT,tf INTEGER,signal INTEGER,side TEXT,deals TEXT,PRIMARY KEY(symbol,tf,signal,side));
                  CREATE TABLE retest_windows(symbol TEXT,tf INTEGER,signal INTEGER,PRIMARY KEY(symbol,tf,signal));
                  CREATE TABLE pair_finished(symbol TEXT,tf INTEGER,signal INTEGER,outcome TEXT,PRIMARY KEY(symbol,tf,signal));""")
                c.execute("INSERT INTO retest_windows VALUES(?,?,?)", ("X", 5, T))
                c.execute(
                    "INSERT INTO retest_settled VALUES(?,?,?,?,?)",
                    ("X", 5, T, "SELL", '["1"]'),
                )
                c.execute(
                    "INSERT INTO pair_finished VALUES(?,?,?,?)",
                    ("X", 5, T, "demo_batch_sent"),
                )
                c.commit()
            for _ in range(2):
                with closing(db_open(path)) as c:
                    self.assertEqual(get(c, "until"), 777)
                    self.assertEqual(
                        c.execute("SELECT child FROM retest_windows").fetchall(), [(3,)]
                    )
                    self.assertEqual(
                        c.execute("SELECT child FROM pair_finished").fetchall(), [(0,)]
                    )
                    index_retest(
                        c,
                        {
                            "event": "retest_batch_settled",
                            "symbol": "X",
                            "tf": 5,
                            "trigger_tf": 4,
                            "signal": T,
                            "side": -1,
                            "planned": 1,
                            "outcomes": ["filled"],
                            "fill_deals": [2],
                        },
                        MAGIC,
                    )
                    c.commit()
                    self.assertEqual(
                        c.execute(
                            "SELECT child,deals FROM retest_settled ORDER BY child"
                        ).fetchall(),
                        [(3, '["1"]'), (4, '["2"]')],
                    )
