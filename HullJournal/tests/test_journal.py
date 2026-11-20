"""Run with python -m unittest discover -s tests -v (no terminal or account required)."""

import ast
import csv
import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fixtures import BIG, MAGIC, FakeMT, T, deal
from journal import Desktop, Reporter, main, make_server, single_writer
from ledger import (
    FIELDS,
    JournalError,
    cell,
    csv_bytes,
    db_open,
    get,
    identity,
    make_views,
    sync,
)


def ticket(
    pid=401,
    leg=1,
    tf=2,
    stamp=T,
    sy="XAUUSDm",
    side=0,
    end=None,
    gross=8,
    tag_override=None,
    delay=1,
):
    c = f"h{tf}_{stamp}_{leg}" if tag_override is None else tag_override
    return [
        deal(
            BIG + pid * 2,
            pid,
            stamp + delay,
            side,
            0,
            0.2,
            103,
            sy=sy,
            comment=c,
            commission=-0.25,
        ),
        deal(
            BIG + pid * 2 + 1,
            pid,
            end or stamp + 180,
            1 - side,
            1,
            0.2,
            104,
            sy=sy,
            magic=0,
            comment="manual",
            profit=gross,
            commission=-0.25,
            reason=0,
        ),
    ]


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.c = db_open(self.folder / "test.sqlite")
        self.addCleanup(lambda: self.c.close())

    def read(self, mt, when=None, **kw):
        if when is not None:
            mt.at = when
        sync(self.c, mt, mt.at, **kw)
        return make_views(self.c)

    def logpath(self, mt):
        path = (
            self.folder
            / "reports"
            / "hull_runtime"
            / identity(mt, MAGIC)["key"]
            / "demo.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def emit(self, path, ev):
        with path.open("a", encoding="utf8") as f:
            f.write(json.dumps(ev) + "\n")

    def receipt(self, ds, tag=None):
        d = ds[0]
        return {
            "event": "demo_result",
            "symbol": d.symbol,
            "deal": d.ticket,
            "signal": T,
            "leg": 1,
            "request": {
                "symbol": d.symbol,
                "magic": MAGIC,
                "type": d.type,
                "comment": tag or f"h2_{T}_1",
                "sl": 99.2,
                "tp": 110,
            },
        }

    def test_partial_weighted_fills_manual_close_costs_and_linked_adjustment(self):
        p = 457
        tag = f"h5_{T}_1"
        ds = [
            deal(13, p, T + 1, 0, 0, 0.1, 100, comment=tag, commission=-0.2),
            deal(19, p, T + 2, 0, 0, 0.2, 103, comment=tag, commission=-0.4),
            deal(20, p, T + 7, 1, 1, 0.1, 106, magic=0, commission=-0.1, profit=4.2),
            deal(
                40,
                p,
                T + 70,
                1,
                1,
                0.2,
                98,
                magic=0,
                commission=-0.2,
                profit=-3.1,
                fee=-0.05,
                swap=-0.3,
                reason=4,
                ms=113,
            ),
            deal(41, p, T + 72, 12, 0, 0, 0, fee=-0.1),
        ]
        mt = FakeMT(ds, T + 20)
        rows, counts, _ = self.read(mt)
        self.assertEqual(counts["open_tickets"], 1)
        self.assertFalse(rows["tickets"])
        self.assertFalse(rows["setups"])
        rows, _, _ = self.read(mt, T + 74)
        r = rows["tickets"][0]
        self.assertEqual(
            (r["position_id"], r["timeframe"], r["side"], r["leg"]),
            ("457", "M5", "BUY", "1"),
        )
        self.assertEqual(
            (r["lots"], r["open_price"], r["close_price"]),
            ("0.3", "102", "100.6666666667"),
        )
        self.assertEqual(
            [
                r[k]
                for k in (
                    "gross_profit",
                    "commission",
                    "swap",
                    "fee",
                    "net_profit",
                    "outcome",
                )
            ],
            ["1.1", "-0.9", "-0.3", "-0.15", "-0.25", "Loss"],
        )
        self.assertEqual(
            (r["duration_s"], r["exit_reason"], r["exit_deals"]),
            ("69.113", "Stop loss", "20;40"),
        )
        self.assertEqual(rows["setups"][0]["net_profit"], "-0.25")

    def test_ticket_now_setup_only_after_final_leg(self):
        mt = FakeMT(ticket(end=T + 82) + ticket(402, 2, end=T + 190, gross=-10), T + 90)
        rows, stats, _ = self.read(mt)
        self.assertEqual([len(x) for x in rows.values()], [1, 0])
        self.assertEqual(stats["pending_setups"], 1)
        rows, _, _ = self.read(mt, T + 200)
        self.assertEqual([len(x) for x in rows.values()], [2, 1])
        r = rows["setups"][0]
        self.assertEqual(
            (r["ticket_count"], r["lots"], r["net_profit"], r["outcome"]),
            ("2", "0.4", "-3.00", "Loss"),
        )
        self.assertEqual(r["close_time_utc"], rows["tickets"][-1]["close_time_utc"])

    def test_entry_window_does_not_predeclare_fast_batch(self):
        ds = ticket(end=T + 2) + ticket(402, leg=2, delay=6, end=T + 13)
        mt = FakeMT(ds, T + 4)
        rows, _, due = self.read(mt)
        self.assertEqual(len(rows["tickets"]), 1)
        self.assertFalse(rows["setups"])
        self.assertEqual(due, T + 60)
        self.assertFalse(self.read(mt, T + 20)[0]["setups"])
        self.assertEqual(self.read(mt, T + 60)[0]["setups"][0]["ticket_count"], "2")

    def test_live_quote_exact_receipt_join_before_bar_end_no_confirmed_close(self):
        from ledger import iso

        ms = (T + 2) * 1000 + 360
        ds = ticket(end=T + 5, delay=3, tag_override="broker changed")
        mt = FakeMT(ds, T + 6)
        log = self.logpath(mt)
        self.emit(log, self.receipt(ds, f"i2_{ms}_1"))
        self.emit(
            log,
            dict(
                event="live_hull_cross",
                symbol="XAUUSDm",
                tf=2,
                entry_side=1,
                entry_setup="bull_protected_pullback_buy",
                close_s=T + 120,
                trigger_msc=ms,
                trigger={"ms": ms, "bid": 103.22, "ask": 103.32},
            ),
        )
        self.emit(
            log,
            dict(
                event="label_decision",
                symbol="XAUUSDm",
                tf=2,
                close_s=T + 120,
                trigger_msc=ms,
                entry_timing="live_hull_cross",
                outcome="demo_batch_sent",
            ),
        )
        rows, _, _ = self.read(mt, repo=self.folder)
        for r in (rows["tickets"][0], rows["setups"][0]):
            self.assertEqual(
                (r["entry_timing"], r["signal_trigger_utc"], r["signal_close_utc"]),
                ("live_hull_cross", iso(ms), ""),
            )
            self.assertEqual(
                (
                    r["signal_bar_open_utc"],
                    r["signal_bar_end_utc"],
                    r["trigger_bid"],
                    r["trigger_ask"],
                ),
                (iso(T * 1000), iso((T + 120) * 1000), "103.22", "103.32"),
            )
            self.assertEqual(
                (r["open_price"], r["setup_type"], r["timeframe"]),
                ("103", "bull_protected_pullback_buy", "M2"),
            )
        self.assertEqual(rows["tickets"][0]["attribution"], "Bot receipt")
        self.assertEqual(rows["setups"][0]["ticket_count"], "1")

    def test_live_comment_alone_never_guesses_model_or_confuses_trigger_with_close(
        self,
    ):
        from ledger import iso

        ms = T * 1000 + 360
        live = ticket(tag_override=f"i2_{ms}_1")
        other = ticket(pid=500, stamp=T + 120)
        mt = FakeMT(live + other, T + 350)
        log = self.logpath(mt)
        self.emit(
            log,
            dict(
                event="closed_label",
                symbol="XAUUSDm",
                tf=2,
                close_s=T + 120,
                entry_side=1,
                entry_setup="bull_choch_buy",
            ),
        )
        rows, _, _ = self.read(mt, repo=self.folder)
        for group in rows.values():
            a, b = sorted(group, key=lambda r: r["open_time_utc"])
            self.assertNotEqual(a["setup_id"], b["setup_id"])
            self.assertEqual(
                (
                    a["signal_trigger_utc"],
                    a["entry_timing"],
                    a["signal_close_utc"],
                    a["setup_type"],
                    a["signal_bar_end_utc"],
                    a["trigger_ask"],
                ),
                (iso(ms), "live_hull_cross", "", "", "", ""),
            )
            self.assertEqual(
                (b["signal_close_utc"], b["entry_timing"], b["setup_type"]),
                (iso((T + 120) * 1000), "confirmed_close", "bull_choch_buy"),
            )

    def test_live_trigger_second_window_recovers_older_sibling_without_ms_as_seconds(
        self,
    ):
        ms = T * 1000 + 360
        early = ticket(end=T + 8, tag_override=f"i2_{ms}_1")
        late = ticket(402, 2, end=T + 5 * 86400, tag_override=f"i2_{ms}_2")
        rows, _, _ = self.read(FakeMT(early + late, T + 5 * 86400 + 10), days=1)
        self.assertEqual(len(rows["tickets"]), 2)
        self.assertEqual(rows["setups"][0]["ticket_count"], "2")
        self.assertEqual(rows["setups"][0]["net_profit"], "15.00")

    def test_actual_partial_batch_not_planned_count(self):
        ds = ticket(end=T + 4)
        mt = FakeMT(ds, T + 6)
        log = self.logpath(mt)
        self.emit(log, {"event": "batch_preview", "symbol": ds[0].symbol, "legs": 10})
        self.emit(log, self.receipt(ds))
        self.emit(
            log,
            {
                "event": "label_decision",
                "symbol": ds[0].symbol,
                "tf": 2,
                "close_s": T,
                "outcome": "protected_partial_no_topup:spread_cap",
            },
        )
        rows, _, _ = self.read(mt, repo=self.folder)
        self.assertEqual(rows["setups"][0]["ticket_count"], "1")

    def test_unknown_never_guessed_or_grouped(self):
        ds = ticket() + ticket(402, 2, tag_override="broker replaced")
        mt = FakeMT(ds, T + 200)
        rows, counts, _ = self.read(mt)
        self.assertEqual(len(rows["tickets"]), 2)
        r = next(x for x in rows["tickets"] if x["position_id"] == "402")
        self.assertEqual((r["timeframe"], r["setup_id"]), ("Unknown", ""))
        self.assertEqual(counts["unattributed"], 1)
        self.assertFalse(rows["setups"])

    def test_exact_log_fallback_and_incremental_partial_line(self):
        ds = ticket(tag_override="broker changed")
        mt = FakeMT(ds, T + 200)
        p = self.logpath(mt)
        self.emit(
            p,
            {
                "event": "closed_label",
                "symbol": "XAUUSDm",
                "tf": 2,
                "close_s": T,
                "entry_side": 1,
                "entry_setup": "bull_discount_buy",
            },
        )
        b = json.dumps(self.receipt(ds))
        p.write_bytes(p.read_bytes() + b.encode())
        rows, _, _ = self.read(mt, repo=self.folder)
        self.assertEqual(rows["tickets"][0]["timeframe"], "Unknown")
        p.write_bytes(p.read_bytes() + b"\n")
        rows, _, _ = self.read(mt, T + 201, repo=self.folder)
        r = rows["tickets"][0]
        self.assertEqual(
            (r["timeframe"], r["initial_sl"], r["setup_type"], r["attribution"]),
            ("M2", "99.2", "bull_discount_buy", "Bot receipt"),
        )
        self.assertEqual(r["entry_deals"], str(ds[0].ticket))
        self.assertEqual(len(rows["setups"]), 1)

    def test_missing_broker_receipt_prevents_false_completion(self):
        d1 = ticket()
        d2 = ticket(489, 2)
        mt = FakeMT(d1, T + 220)
        p = self.logpath(mt)
        self.emit(p, self.receipt(d2, f"h2_{T}_2"))
        rows, s, _ = self.read(mt, repo=self.folder)
        self.assertEqual(s["unmatched_receipts"], 1)
        self.assertFalse(rows["setups"])
        mt.ds.extend(d2)
        rows, s, _ = self.read(mt, T + 221, repo=self.folder)
        self.assertEqual(s["unmatched_receipts"], 0)
        self.assertEqual(rows["setups"][0]["ticket_count"], "2")

    def test_cash_flows_foreign_magic_and_mixed_ownership(self):
        foreign = ticket(505, sy="EURUSDm")
        foreign[0].magic = 123
        ds = ticket() + foreign + [deal(21, 0, T, 2, 0, 0, 0, profit=20000)]
        mt = FakeMT(ds, T + 220)
        rows, _, _ = self.read(mt)
        self.assertEqual(len(rows["tickets"]), 1)
        self.assertEqual(rows["tickets"][0]["net_profit"], "7.50")
        mt.ds += [deal(342, 401, T + 3, 0, 0, 0.02, 103, magic=22)]
        rows, s, _ = self.read(mt, T + 221)
        self.assertFalse(rows["tickets"])
        self.assertEqual(s["review"], 1)

    def test_broker_correction_restart_repeat_and_spread_not_subtracted_twice(self):
        ds = ticket()
        mt = FakeMT(ds, T + 185)
        rows, _, _ = self.read(mt)
        before = csv_bytes(rows["tickets"])
        self.c.close()
        self.c = db_open(self.folder / "test.sqlite")
        self.assertEqual(csv_bytes(self.read(mt, T + 186)[0]["tickets"]), before)
        mt.ds[1].fee = -0.3
        rows, _, _ = self.read(mt, T + 187)
        self.assertEqual(rows["tickets"][0]["net_profit"], "7.20")
        self.assertEqual(self.c.execute("SELECT count(*) FROM deals").fetchone()[0], 2)

    def test_late_correction_outside_window_explicit_reconcile(self):
        ds = ticket()
        mt = FakeMT(ds, T + 200)
        self.read(mt, days=1)
        self.read(mt, T + 3 * 86400, days=1)
        mt.ds[0].fee = -1.2
        rows, _, _ = self.read(mt, T + 3 * 86400 + 10, days=1, reconcile=True)
        self.assertEqual(rows["tickets"][0]["net_profit"], "6.30")

    def test_long_lived_position_recovers_older_siblings_before_lookback(self):
        early = ticket(end=T + 80)
        later = ticket(402, 2, end=T + 5 * 86400)
        mt = FakeMT(early + later, T + 5 * 86400 + 10)
        rows, _, _ = self.read(mt, days=1)
        self.assertEqual(len(rows["tickets"]), 2)
        self.assertEqual(rows["setups"][0]["ticket_count"], "2")
        self.assertEqual(rows["setups"][0]["net_profit"], "15.00")

    def test_close_by_breakeven_unrelated_symbol_and_lane(self):
        a = ticket(gross=0.5)
        a[1].entry = 3
        b = ticket(411, tf=5, sy="GBPJPYm", side=1)
        d = ticket(412, tf=1)
        mt = FakeMT(a + b + d, T + 220)
        rs, _, _ = self.read(mt)
        self.assertEqual(len(rs["setups"]), 3)
        x = next(r for r in rs["tickets"] if r["position_id"] == "401")
        self.assertEqual(
            (x["net_profit"], x["exit_reason"], x["outcome"]),
            ("0.00", "Close by", "Breakeven"),
        )
        self.assertEqual({r["timeframe"] for r in rs["setups"]}, {"M2", "M5", "M1"})

    def test_missing_history_and_changed_account_never_advance_or_overwrite(self):
        ds = ticket()
        mt = FakeMT(ds, T + 80)
        self.read(mt)
        last = get(self.c, "until")
        mt.at = T + 210
        for stage in ("range", "positions", "position"):
            mt.missing = stage
            with self.assertRaises(JournalError):
                sync(self.c, mt, mt.at)
            self.assertEqual(get(self.c, "until"), last)
            self.assertFalse(make_views(self.c)[0]["tickets"])
        mt.missing = None
        mt.login += 1
        with self.assertRaisesRegex(JournalError, "changed"):
            sync(self.c, mt, mt.at)
        mt.login -= 1
        self.assertEqual(len(self.read(mt)[0]["tickets"]), 1)

    def test_mid_poll_account_switch_rolls_back(self):
        mt = FakeMT(ticket(), T + 220)
        real = mt.positions_get

        def switch():
            mt.login += 1
            return real()

        mt.positions_get = switch
        with self.assertRaisesRegex(JournalError, "changed during"):
            sync(self.c, mt, mt.at)
        self.assertIsNone(get(self.c, "identity"))
        self.assertEqual(self.c.execute("SELECT count(*) FROM deals").fetchone()[0], 0)

    def test_missing_open_history_blocks_sibling_until_resolved(self):
        ds = ticket(end=T + 70)
        mt = FakeMT(ds, T + 100)
        mt.ps_override = [
            NS(identifier=584, ticket=8584, time=T + 3, symbol="XAUUSDm", magic=MAGIC)
        ]
        rows, stats, _ = self.read(mt)
        self.assertEqual(len(rows["tickets"]), 1)
        self.assertFalse(rows["setups"])
        self.assertEqual(stats["review"], 1)
        mt.ds += ticket(584, 2, end=T + 110, delay=3)
        mt.ps_override = None
        rows, stats, _ = self.read(mt, T + 120)
        self.assertEqual(rows["setups"][0]["ticket_count"], "2")

    def test_exit_missing_and_still_listed_live_never_win(self):
        mt = FakeMT(ticket(), T + 210)
        mt.ps_override = [
            NS(identifier=401, ticket=8251, magic=MAGIC, symbol="XAUUSDm")
        ]
        self.assertFalse(self.read(mt)[0]["tickets"])
        mt.ps_override = ()
        mt.ds = mt.ds[
            :1
        ]  # previously verified close is retained; test a new incomplete position
        mt.ds += [ticket(900)[0]]
        rows, s, _ = self.read(mt, T + 211)
        self.assertEqual(s["review"], 1)
        self.assertNotIn("900", [r["position_id"] for r in rows["tickets"]])

    def test_netting_inout_or_cancelled_deal_quarantined(self):
        for typ, ent in ((1, 2), (14, 1)):
            ds = ticket(100 + typ)
            ds[1].type = typ
            ds[1].entry = ent
            mt = FakeMT(ds, T + 220)
            rows, s, _ = self.read(mt)
            self.assertFalse(rows["tickets"])
            self.assertGreaterEqual(s["review"], 1)
        mt.account_info = lambda: NS(
            login=1,
            server="Fake",
            currency="USD",
            currency_digits=2,
            trade_mode=2,
            margin_mode=0,
        )
        with self.assertRaisesRegex(JournalError, "DEMO"):
            sync(self.c, mt, mt.at)

    def test_csv_schema_excel_roundtrip_and_text_injection(self):
        ds = ticket(sy='=ABC,"quoted"')
        rs = self.read(FakeMT(ds, T + 210))[0]["tickets"]
        blob = csv_bytes(rs)
        self.assertTrue(blob.startswith(b"\xef\xbb\xbf"))
        f = csv.DictReader(io.StringIO(blob.decode("utf-8-sig")))
        self.assertEqual(f.fieldnames, FIELDS)
        self.assertEqual(next(f)["symbol"], '\'=ABC,"quoted"')
        self.assertEqual(cell("commission", "-0.1"), "-0.1")

    def test_csv_export_retries_atomic_lock_and_skips_unchanged_writes(self):
        mt = FakeMT(ticket(), T + 220)
        ui = Desktop()
        rp = Reporter(self.c, self.folder, ui)
        import journal

        real = journal.atomic

        def locked(p, b):
            if p.name == "setups.csv":
                raise PermissionError("held by spreadsheet")
            real(p, b)

        with patch("journal.atomic", locked), self.assertRaises(PermissionError):
            rp.poll(mt, mt.at)
        self.assertTrue(rp.rebuild)
        self.assertFalse(ui.rows["tickets"])
        rp.poll(mt, mt.at + 1)
        self.assertFalse(rp.rebuild)
        self.assertEqual(len(ui.rows["setups"]), 1)
        p = self.folder / "tickets.csv"
        modified = p.stat().st_mtime_ns
        with patch("journal.atomic", side_effect=AssertionError("unnecessary write")):
            rp.poll(mt, mt.at + 2)
        self.assertEqual(p.stat().st_mtime_ns, modified)
        self.assertEqual(list(self.folder.glob("*.tmp")), [])

    def test_http_private_routes_since_and_writer_lock(self):
        ui = Desktop()
        srv = make_server(ui, 0)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        base = f"http://127.0.0.1:{srv.server_port}"
        try:
            with urlopen(base) as r:
                self.assertIn(b'data-feed="collector"', r.read())
                self.assertEqual(r.headers["Cache-Control"], "no-store")
            with urlopen(base + "/api/snapshot") as r:
                self.assertEqual(json.load(r)["tickets"], [])
            with urlopen(base + "/api/snapshot?since=initial") as r:
                self.assertNotIn("tickets", json.load(r))
            for path, host, code in [
                ("/journal.sqlite3", None, 404),
                ("/", "other.example", 403),
            ]:
                req = Request(base + path, headers={"Host": host} if host else {})
                with self.assertRaises(HTTPError) as m:
                    urlopen(req)
                self.assertEqual(m.exception.code, code)
        finally:
            srv.shutdown()
            srv.server_close()
            th.join()
        with (
            single_writer(self.folder),
            self.assertRaises(JournalError),
            single_writer(self.folder),
        ):
            pass

    def test_collector_downtime_longer_than_rolling_window(self):
        mt = FakeMT(ticket(), T + 220)
        self.read(mt, days=1)
        mt.ds += ticket(998, stamp=T + 2 * 86400) + ticket(999, stamp=T + 4 * 86400)
        r, _, _ = self.read(mt, T + 6 * 86400, days=1)
        self.assertEqual(
            {x["position_id"] for x in r["tickets"]}, {"401", "998", "999"}
        )
        self.assertEqual(len(r["setups"]), 3)

    def test_unresolved_sibling_disappears_before_history_is_published(self):
        mt = FakeMT(ticket(), T + 210)
        mt.ps_override = [
            NS(identifier=404, ticket=404, symbol="XAUUSDm", magic=MAGIC, time=T + 3)
        ]
        self.assertFalse(self.read(mt)[0]["setups"])
        mt.ps_override = ()
        self.assertFalse(self.read(mt, T + 220)[0]["setups"])
        mt.ds += ticket(404, leg=2, end=T + 210, delay=3)
        self.assertEqual(self.read(mt, T + 240)[0]["setups"][0]["ticket_count"], "2")

    def test_cli_one_shot_and_non_journal_collision(self):
        mt = FakeMT(ticket(), T + 220)
        mt.initialize = lambda *a: True
        mt.shutdown = lambda: None
        dest = self.folder / "cli"
        bogus = self.folder / "unowned"
        bogus.mkdir()
        (bogus / "tickets.csv").write_bytes(b"keep")
        with (
            patch.dict(sys.modules, {"MetaTrader5": mt}),
            patch("journal.time.time", return_value=mt.at),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(["--once", "--out", str(dest)]), 0)
            self.assertEqual(main(["--once", "--out", str(dest)]), 0)
            self.assertEqual(main(["--once", "--out", str(bogus)]), 1)
        with (dest / "tickets.csv").open(encoding="utf-8-sig", newline="") as f:
            self.assertEqual([r["net_profit"] for r in csv.DictReader(f)], ["7.50"])
        self.assertEqual((bogus / "tickets.csv").read_bytes(), b"keep")

    def test_only_read_apis_no_trading_calls(self):
        root = Path(__file__).resolve().parents[1]
        attrs = set()
        for name in ["journal.py", "ledger.py"]:
            for n in ast.walk(ast.parse((root / name).read_text(encoding="utf8"))):
                if (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "mt"
                ):
                    attrs.add(n.func.attr)
        self.assertEqual(
            attrs,
            {
                "initialize",
                "shutdown",
                "terminal_info",
                "account_info",
                "history_deals_get",
                "positions_get",
            },
        )


if __name__ == "__main__":
    unittest.main()
