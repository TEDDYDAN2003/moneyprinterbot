"""The receipt, not today's preset, is evidence of a historical ticket's cash goal."""

import csv
import io
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fixtures import MAGIC, FakeMT, T, deal
from ledger import csv_bytes, db_open, get, import_log, make_views, put, sync
from test_spread_receipts import market_receipt


def economics(e, base, spread, leg=1):
    return e | {
        "target_policy": "base_plus_entry_spread_v1",
        "configured_target_usd": base,
        "entry_spread_usd": spread,
        "initial_goal_usd": base + spread,
        "planned_tp_profit_usd": base + spread + 0.02,
        "price": 100,
        "fill_volume": 0.1,
        "opening_order": e["deal"] + 100,
        "leg": leg,
        "request": e["request"] | {"volume": 0.1, "comment": f"p5x4_{T}_{leg}"},
    }


def history(events):
    deals = []
    for e in events:
        n = e["deal"]
        deals += [
            deal(n, n, T + 1, 1, 0, 0.1, 100, comment=e["request"]["comment"]),
            deal(n + 20, n, T + 10, 0, 1, 0.1, 98, profit=2, magic=0),
        ]
    return FakeMT(deals, T + 200)


class TargetEvidenceTests(unittest.TestCase):
    def test_upgrade_replay_reload_csv_mixed_legs_and_missing_evidence(self):
        events = [
            economics(market_receipt(did=i), 17.25, spread, leg=i)
            for i, spread in [(1, 1.5), (2, 2.5)]
        ]
        mt = history(events)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.sqlite3"
            log = Path(tmp) / "demo.jsonl"
            log.write_text("".join(json.dumps(z) + "\n" for z in events))
            with closing(db_open(path)) as db:
                import_log(db, log, MAGIC)
                sync(db, mt, mt.at)
                with db:
                    db.execute("DROP TABLE target_receipts")
                    put(db, "log_cursor", get(db, "log_cursor") | {"format": 10})
                previous = get(db, "until")
            with closing(db_open(path)) as db:
                import_log(db, log, MAGIC)
                self.assertEqual(get(db, "until"), previous)
                db.commit()
                rows = make_views(db)[0]
                self.assertEqual(
                    [r["initial_goal_usd"] for r in rows["tickets"]], ["18.75", "19.75"]
                )
                (setup,) = rows["setups"]
                self.assertEqual(
                    (
                        setup["configured_target_usd"],
                        setup["initial_goal_usd"],
                        setup["batch_initial_goal_usd"],
                        setup["net_profit"],
                    ),
                    ("17.25", "", "38.5", "4.00"),
                )
                exported = list(
                    csv.DictReader(
                        io.StringIO(csv_bytes(rows["tickets"]).decode("utf-8-sig"))
                    )
                )
                self.assertEqual(
                    [row["entry_spread_usd"] for row in exported], ["1.5", "2.5"]
                )
            # A second contradictory base + mathematically valid goal must not win, even on reindex.
            with log.open("a") as f:
                for ev in [economics(events[0], 99, 3), events[0]]:
                    print(json.dumps(ev), file=f)
            with closing(db_open(path)) as db:
                import_log(db, log, MAGIC)
                rows = make_views(db)[0]
                self.assertEqual(
                    [r["initial_goal_usd"] for r in rows["tickets"]], ["", "19.75"]
                )
                self.assertEqual(rows["setups"][0]["batch_initial_goal_usd"], "")
                self.assertEqual(rows["setups"][0]["net_profit"], "4.00")

    def test_rejections_previews_partial_or_mismatched_fill_never_supply_a_target(self):
        ref = economics(market_receipt(), 7, 1)
        for changes in (
            {"event": "batch_preview"},
            {"retcode": 10010},
            {"target_policy": "unknown"},
            {"price": 99},
            {"opening_order": 77},
            {"fill_volume": 0.05},
            {"entry_spread_usd": -1},
            {"initial_goal_usd": 7},
            {"planned_tp_profit_usd": 6},
            {"configured_target_usd": None},
        ):
            with (
                self.subTest(changes=changes),
                tempfile.TemporaryDirectory() as tmp,
                closing(db_open(":memory:")) as db,
            ):
                log = Path(tmp) / "demo.jsonl"
                log.write_text(json.dumps(ref | changes) + "\n")
                mt = history([ref])
                import_log(db, log, MAGIC)
                sync(db, mt, mt.at)
                (ticket,) = make_views(db)[0]["tickets"]
                self.assertEqual(
                    (
                        ticket["target_policy"],
                        ticket["initial_goal_usd"],
                        ticket["net_profit"],
                    ),
                    ("", "", "2.00"),
                )
