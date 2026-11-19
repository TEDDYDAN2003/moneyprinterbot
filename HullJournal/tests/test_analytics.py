"""Trade dimensions and non-overlapping market windows (all inputs are synthetic)."""

import csv
import io
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fixtures import FakeMT, T
from hull_analytics import entry_reason, session_minute
from ledger import csv_bytes, db_open, make_views, sync
from test_journal import ticket


def classify(s):
    return session_minute(
        int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp()) // 60
    )


def zones_available():
    try:
        for city in [
            "Australia/Sydney",
            "Asia/Tokyo",
            "Europe/London",
            "America/New_York",
        ]:
            ZoneInfo(city)
    except ZoneInfoNotFoundError:
        return False
    return True


class AnalyticsTests(unittest.TestCase):
    @unittest.skipUnless(
        zones_available(), "Install tzdata to exercise IANA transitions."
    )
    def test_us_eu_staggered_dst_and_half_open_bounds(self):
        for stamp, expected in [
            ("2026-01-12T12:59", "London"),
            ("2026-01-12T13:00", "London + New York"),
            ("2026-07-13T12:00", "London + New York"),
            ("2026-03-16T12:00", "London + New York"),
            ("2026-03-16T16:30", "London + New York"),
            ("2026-03-30T16:30", "New York"),
            ("2026-01-12T08:59", "Tokyo + London"),
            ("2026-01-12T09:00", "London"),
        ]:
            self.assertEqual(classify(stamp), expected, stamp)

    @unittest.skipUnless(
        zones_available(), "Install tzdata to exercise IANA transitions."
    )
    def test_weekends_and_sydney_southern_hemisphere_dst(self):
        for stamp, expected in [
            ("2026-01-17T10:00", "Off-session"),
            ("2026-11-08T21:00", "Sydney"),
            ("2026-07-12T21:00", "Off-session"),
            ("2026-07-12T22:00", "Sydney"),
        ]:
            self.assertEqual(classify(stamp), expected, stamp)

    def test_missing_zones_never_guessed(self):
        session_minute.cache_clear()
        try:
            with patch(
                "hull_analytics.ZoneInfo", side_effect=ZoneInfoNotFoundError("test")
            ):
                self.assertEqual(
                    classify("2026-07-13T07:04"), "Timezone data unavailable"
                )
        finally:
            session_minute.cache_clear()

    def test_new_structure_reasons_do_not_rewrite_legacy_reasons(self):
        self.assertEqual(
            entry_reason("bear_protected_pullback_sell"),
            "SELL · pullback below protected LH",
        )
        self.assertEqual(
            entry_reason("bull_protected_pullback_buy"),
            "BUY · pullback above protected HL",
        )
        self.assertEqual(entry_reason("bear_choch_sell"), "SELL · bearish CHoCH close")
        self.assertEqual(entry_reason("bull_choch_buy"), "BUY · bullish CHoCH close")
        self.assertEqual(entry_reason("bear_premium_sell"), "SELL · bearish premium")

    def test_reason_from_matching_signal_not_trade_outcome(self):
        ds = ticket(end=T + 180, gross=-8)
        ds[-1].reason = 4
        ds[-1].comment = "hull_exit"
        c = db_open(":memory:")
        self.addCleanup(c.close)
        mt = FakeMT(ds, T + 200)
        sync(c, mt, mt.at)
        with c:
            c.execute(
                "insert into labels values(?,?,?,?,?)",
                ("XAUUSDm", 5, T, "BUY", "bull_discount_buy"),
            )
        rows = make_views(c)[0]
        self.assertEqual(rows["tickets"][0]["entry_reason"], "Not recorded")
        self.assertEqual(rows["tickets"][0]["exit_reason"], "Stop loss")
        with c:
            c.execute(
                "insert into labels values(?,?,?,?,?)",
                ("XAUUSDm", 2, T, "BUY", "buy_below_bear_bos"),
            )
        for rs in make_views(c)[0].values():
            row = rs[0]
            self.assertEqual(row["entry_reason"], "BUY · below bearish BOS")
            self.assertEqual(row["outcome"], "Loss")
            self.assertTrue(row["open_session"])
            self.assertTrue(row["close_session"])
            self.assertEqual(
                next(csv.DictReader(io.StringIO(csv_bytes(rs).decode("utf-8-sig"))))[
                    "entry_reason"
                ],
                row["entry_reason"],
            )
        self.assertEqual(
            entry_reason("custom_logged_rule"), "Recorded: custom logged rule"
        )

    def test_entry_exit_session_classification_can_differ_and_setups_have_two_endpoints(
        self,
    ):
        stamp = int(datetime(2026, 1, 12, 8, 59, tzinfo=UTC).timestamp())
        ds = ticket(stamp=stamp, end=stamp + 65) + ticket(
            402, 2, stamp=stamp, delay=3, end=stamp + 6 * 3600
        )
        mt = FakeMT(ds, stamp + 6 * 3600 + 1)
        c = db_open(":memory:")
        self.addCleanup(c.close)
        sync(c, mt, mt.at)
        rs = make_views(c)[0]
        self.assertEqual(len(rs["tickets"]), 2)
        self.assertEqual(len(rs["setups"]), 1)
        if zones_available():
            self.assertEqual(
                [r["close_session"] for r in rs["tickets"]],
                ["London", "London + New York"],
            )
            self.assertEqual(
                (rs["setups"][0]["open_session"], rs["setups"][0]["close_session"]),
                ("Tokyo + London", "London + New York"),
            )
        self.assertEqual(
            sum(Decimal(r["gross_profit"]) for r in rs["tickets"]),
            Decimal(rs["setups"][0]["gross_profit"]),
        )

    def test_logged_exit_request_is_extra_evidence_not_cause_of_broker_fill(self):
        import json
        import tempfile
        from pathlib import Path

        from ledger import MAGIC, identity, iso

        ds = ticket()
        ds[-1].comment = "hull_exit"
        ds[-1].reason = 3
        mt = FakeMT(ds, T + 210)
        c = db_open(":memory:")
        self.addCleanup(c.close)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            p = (
                root
                / "reports"
                / "hull_runtime"
                / identity(mt, MAGIC)["key"]
                / "demo.jsonl"
            )
            p.parent.mkdir(parents=True)

            def log(pid, code, delta, reply=10009):
                with p.open("a") as f:
                    f.write(
                        json.dumps(
                            {
                                "event": "exit_request",
                                "symbol": "XAUUSDm",
                                "at": iso(ds[-1].time_msc + delta),
                                "ticket": pid,
                                "reason": code,
                                "retcode": reply,
                            }
                        )
                        + "\n"
                    )

            log(411, "wrong_position", 100)
            log(401, "refused", 20, 10004)
            log(401, "stale", -61000)
            sync(c, mt, mt.at, repo=root)
            self.assertEqual(make_views(c)[0]["tickets"][0]["bot_exit_request"], "")
            log(401, "opposite_hull_label", 132)
            sync(c, mt, mt.at, repo=root)
            r = make_views(c)[0]["tickets"][0]
            self.assertEqual(
                (r["exit_reason"], r["bot_exit_request"]),
                ("Hull exit", "opposite_hull_label"),
            )
            self.assertTrue(r["bot_exit_request_utc"])
            log(401, "kill", 520)
            sync(c, mt, mt.at, repo=root)
            self.assertEqual(make_views(c)[0]["tickets"][0]["bot_exit_request"], "")
