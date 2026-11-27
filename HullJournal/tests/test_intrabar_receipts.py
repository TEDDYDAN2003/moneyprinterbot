"""Intrabar receipt evidence is optional and disjoint from closed-wick proof."""

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fixtures import T
from ledger import MAGIC, db_open, get, import_log, tag
from test_spread_receipts import ingest, market_receipt


def entry():
    e = market_receipt(child=4)
    for k in (
        "cross_candle_open_s",
        "cross_candle_wick",
        "frozen_cross_sl",
        "close_to_submit_ms",
    ):
        del e[k]
    stamp = T * 1000 + 125
    e.update(
        entry_policy="intrabar_pair_structure_cross_v1",
        entry_timing="intrabar_cross",
        initial_stop_policy="confirmed_trigger_range_hl_lh_v1",
        trigger_msc=stamp,
        signal=stamp / 1000,
        trigger_bid=100,
        trigger_ask=102,
        swing_kind="LH",
        swing_price=109,
        frozen_swing_sl=110,
        swing_bar_s=T - 960,
        swing_known_s=T - 240,
        origin_bar_end_s=T + 240,
        trigger_to_submit_ms=175,
        opening_order=51,
        fill_volume=0.1,
        price=100,
    )
    e["request"].update(comment=f"q5x4_{stamp}_1", volume=0.1)
    return e


class IntrabarEvidenceTests(unittest.TestCase):
    def test_origin_close_stop_update_is_joined_to_the_exact_entry_deal(self):
        e = entry()
        e.update(
            cross_region_policy="hull_line_intersection_tighten_only_v1",
            hull_cross_price=101,
            cross_region_candle_close=None,
            cross_candle_raw_sl=110,
            cross_region_raw_stop=101,
            cross_region_action="awaiting_origin_close",
            cross_region_stop_status="awaiting_origin_close",
            raw_chart_sl=110,
        )
        with tempfile.TemporaryDirectory() as root, closing(db_open(":memory:")) as db:
            path = Path(root) / "demo.jsonl"
            with path.open("a") as f:
                print(json.dumps(e), file=f)
            import_log(db, path, MAGIC)
            (initial,) = db.execute("SELECT evidence FROM swing_receipts").fetchone()
            self.assertEqual(
                json.loads(initial)["fields"]["cross_region_stop_status"],
                "awaiting_origin_close",
            )
            event = {
                "event": "cross_region_stop_activation",
                "symbol": "XAUUSDm",
                "scanner_magic": MAGIC,
                "entry_deal": e["deal"],
                "cross_region_policy": "hull_line_intersection_tighten_only_v1",
                "hull_cross_price": 101,
                "cross_region_candle_close": 100,
                "cross_region_raw_stop": 101,
                "cross_region_action": "cross_region_tightened",
                "cross_region_status": "active",
                "raw_chart_sl": 101,
            }
            with path.open("a") as f:
                print(json.dumps(event), file=f)
            import_log(db, path, MAGIC)
            (updated,) = db.execute("SELECT evidence FROM swing_receipts").fetchone()
            fields = json.loads(updated)["fields"]
            self.assertEqual(
                (fields["cross_region_action"], fields["cross_region_stop_status"], fields["raw_chart_sl"]),
                ("cross_region_tightened", "active", "101"),
            )

            # Re-indexing an identical opening receipt cannot erase a verified amendment.
            with path.open("a") as f:
                print(json.dumps(e), file=f)
            import_log(db, path, MAGIC)
            self.assertEqual(
                db.execute("SELECT evidence FROM swing_receipts").fetchone(), (updated,)
            )

    def test_frozen_wick_range_validates_inside_or_immediate_outside_without_guessing(
        self,
    ):
        for sign in (-1, 1):
            e = entry()
            cross = 100 - sign * 5
            e.update(
                entry_side=sign,
                parent_structure_bias=sign,
                trigger_structure_bias=sign,
                swing_kind="HL" if sign == 1 else "LH",
                swing_price=100 - sign * 9,
                frozen_swing_sl=100 - sign * 10,
                price=102 if sign == 1 else 100,
                stop_spread_buffer_price=0 if sign == 1 else 2,
                cross_region_policy="hull_line_intersection_tighten_only_v1",
                hull_cross_price=cross,
                cross_region_candle_close=None,
                cross_region_basis="frozen_trigger_bid_wick_range_v2",
                cross_signal_low=98,
                cross_signal_high=102,
                cross_region_location="outside",
                cross_region_raw_stop=cross,
                raw_chart_sl=cross,
                cross_region_stop_status="active",
                cross_region_action="cross_region_tightened",
            )
            e["request"].update(
                type=0 if sign == 1 else 1,
                price=e["price"],
                sl=cross + e["stop_spread_buffer_price"],
                tp=120 if sign == 1 else 80,
            )
            for bad in (
                {},
                {"cross_region_location": "inside"},
                {"cross_signal_low": 101},
                {"cross_region_basis": "guessed"},
                {"cross_region_stop_status": "awaiting_origin_close"},
            ):
                with (
                    self.subTest(sign=sign, bad=bad),
                    tempfile.TemporaryDirectory() as root,
                    closing(db_open(":memory:")) as db,
                ):
                    ingest(db, Path(root) / "demo.jsonl", e | bad)
                    self.assertEqual(
                        len(db.execute("SELECT * FROM swing_receipts").fetchall()),
                        int(not bad),
                    )

    def test_no_mixed_stop_model_or_future_pivot_and_no_bad_time(self):
        e = entry()
        with tempfile.TemporaryDirectory() as root:
            for bad in (
                {},
                {"entry_timing": "confirmed_close"},
                {"swing_known_s": T + 1},
                {"swing_bar_s": T},
                {"origin_bar_end_s": T},
                {"frozen_swing_sl": 111},
                {"swing_price": 100},
                {"initial_stop_policy": "confirmed_pair_trigger_candle_wick_v1"},
                {"swing_kind": "HL"},
                {"frozen_cross_sl": 110},
                {"signal_close_utc": "fake"},
                {"parent_structure_close_s": T + 1},
                {"trigger_structure_bias": 1},
                {"trigger_to_submit_ms": 0},
                {"trigger_msc": e["trigger_msc"] + 1},
                {"send_tick_time_msc": e["trigger_msc"] - 1},
                {"fill_volume": 1},
                {"opening_order": 0},
                {"request": e["request"] | {"sl": 111}},
                {"request": e["request"] | {"price": 99}},
            ):
                with self.subTest(bad=bad), closing(db_open(":memory:")) as db:
                    ingest(db, Path(root) / "demo.jsonl", e | bad)
                    rows = db.execute("SELECT evidence FROM swing_receipts").fetchall()
                    self.assertEqual(len(rows), 0 if bad else 1)
                    self.assertEqual(
                        db.execute("SELECT * FROM cross_receipts").fetchall(), []
                    )
                    self.assertEqual(get(db, "log_cursor")["bad"], int(bool(bad)))
                    if not bad:
                        fields = json.loads(rows[0][0])["fields"]
                        self.assertEqual(
                            (
                                fields["swing_kind"],
                                fields["frozen_swing_sl"],
                                fields["trigger_to_submit_ms"],
                            ),
                            ("LH", "110", "175"),
                        )
                        self.assertNotIn("cross_candle_wick", fields)

    def test_conflicting_proof_cannot_be_reinstated_by_replaying_first_receipt(self):
        e = entry()
        with tempfile.TemporaryDirectory() as root, closing(db_open(":memory:")) as db:
            for i, row in enumerate((e, e | {"swing_price": 108}, e)):
                path = Path(root) / "demo.jsonl"
                with path.open("a") as f:
                    print(json.dumps(row), file=f)
                import_log(db, path, MAGIC)
                proof = json.loads(
                    db.execute("SELECT evidence FROM swing_receipts").fetchone()[0]
                )
                self.assertEqual(proof is None, i > 0)

    def test_pair_tag_clock_namespaces_do_not_alias(self):
        stamp = T * 1000 + 125
        self.assertEqual(tag(f"q5x4_{stamp}_2"), (5, stamp, 2))
        for bad in (
            f"p5x4_{stamp}_2",
            f"q5x4_{T}_2",
            f"q5x8_{stamp}_2",
            f"q30x3_{stamp}_2",
        ):
            self.assertIsNone(tag(bad))
