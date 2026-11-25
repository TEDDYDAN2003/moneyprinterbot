"""All launchers, receipts and accounts here are fabricated. No real subprocess or broker."""

import json
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from demo_control import ControlError, DemoControl, terminal_key
from journal import Desktop, make_server

WHO = ("a" * 16, terminal_key(NS(path=r"C:\MT5", data_path=r"C:\MT5\data")))


class Runner:
    calls = 0
    exit = None
    ack = False
    issues = ()
    error = False

    def review(self, who):
        return {"profiles": [], "issues": list(self.issues), "digest": "f" * 64}

    def start(self, who, digest, rid, expires):
        self.calls += 1
        self.seen = (who, digest, rid)
        self.pid = 123
        if self.error:
            raise OSError("mock failure")
        return NS(pid=self.pid, poll=lambda: self.exit)

    def receipt(self, who, rid):
        return {"state": "started"} if self.ack else {}


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.now = 10
        self.who = WHO
        self.r = Runner()
        self.c = DemoControl(self.r, lambda: self.who, lambda: self.now)

    def check(self):
        return self.c.action("check", {})["ticket"]

    def arm(self, t, confirm="ARM DEMO"):
        return self.c.action("arm", {"ticket": t, "confirmation": confirm})

    def test_review_and_explicit_single_use_launch(self):
        t = self.check()
        self.assertEqual(self.r.calls, 0)
        self.assertNotIn(t, json.dumps(self.c.status()))
        self.assertNotIn("digest", self.c.status()["review"])
        with self.assertRaises(ControlError):
            self.arm(t, "arm")
        with self.assertRaises(ControlError):
            self.arm("other")
        self.arm(t)
        self.assertEqual(self.r.calls, 1)
        self.assertEqual(self.c.status()["phase"], "starting")
        self.r.ack = True
        self.assertEqual(self.c.status()["phase"], "running")
        with self.assertRaises(ControlError):
            self.arm(t)
        with self.assertRaises(ControlError):
            self.check()
        self.r.exit = 1
        self.assertEqual(self.c.status()["phase"], "exited")
        with self.assertRaises(ControlError):
            self.arm(t)
        self.assertEqual(self.r.calls, 1)

    def test_expiry_account_switch_unavailable_and_unknown_ack(self):
        t = self.check()
        self.now += 121
        with self.assertRaises(ControlError):
            self.arm(t)
        t = self.check()
        self.who = ("b" * 16, WHO[1])
        with self.assertRaises(ControlError):
            self.arm(t)
        self.who = None
        with self.assertRaises(ControlError):
            self.check()
        self.who = WHO
        t = self.check()
        self.r.error = True
        with self.assertRaises(ControlError):
            self.arm(t)
        with self.assertRaises(ControlError):
            self.arm(t)
        self.assertEqual(self.r.calls, 1)

    def test_report_cannot_issue_a_ticket_on_any_blocker(self):
        t = self.check()
        self.r.issues = ["hold"]
        self.assertFalse(self.c.action("check", {})["ok"])
        with self.assertRaises(ControlError):
            self.arm(t)
        self.assertEqual(self.r.calls, 0)

    def test_overlapping_requests_are_rejected(self):
        t = self.check()
        hold = threading.Event()
        release = threading.Event()
        orig = self.r.start

        def delayed(*args):
            hold.set()
            release.wait(3)
            return orig(*args)

        self.r.start = delayed
        th = threading.Thread(target=self.arm, args=(t,))
        th.start()
        try:
            self.assertTrue(hold.wait(2))
            with self.assertRaises(ControlError):
                self.arm(t)
            with self.assertRaises(ControlError):
                self.check()
        finally:
            release.set()
            th.join()
        self.assertEqual(self.r.calls, 1)

    def test_collector_authority_expires_and_pauses(self):
        d = Desktop()
        self.assertIsNone(d.control_identity())
        s = {"state": "live", "currency": "USD", "account_key": WHO[0]}
        with patch("journal.time.monotonic", return_value=10):
            d.set(s, terminal=WHO[1])
            self.assertEqual(d.control_identity(), WHO)
        with patch("journal.time.monotonic", return_value=41):
            self.assertIsNone(d.control_identity())
        d.set({"state": "paused"})
        self.assertIsNone(d.control_identity())

    def test_loopback_http_contract(self):
        for controller in [None, self.c]:
            srv = make_server(Desktop(), 0, controller)
            self.assertEqual(srv.server_address[0], "127.0.0.1")
            thread = threading.Thread(target=srv.serve_forever)
            thread.start()
            url = "http://127.0.0.1:" + str(srv.server_port)

            def req(path, data=None, heads=None, url=url):
                q = Request(url + path, data=data, headers=heads or {})
                try:
                    with urlopen(q, timeout=3) as r:
                        return r.status, r.read(), r.headers
                except HTTPError as ex:
                    return ex.code, b"", ex.headers

            try:
                status, body, h = req("/")
                self.assertEqual(status, 200)
                self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])
                self.assertEqual(b"data-demo-key=" in body, controller is not None)
                self.assertEqual(req("/api/demo/arm")[0], 404)
                hd = {
                    "Origin": url,
                    "X-Hull-Control": self.c.key,
                    "Content-Type": "application/json",
                }
                if controller is None:
                    self.assertEqual(req("/api/demo/check", b"{}", hd)[0], 404)
                    self.assertEqual(req("/api/demo/status")[0], 404)
                    continue
                self.assertEqual(
                    req("/api/demo/check", b"{}", {**hd, "Host": "example.invalid"})[0],
                    403,
                )
                for bad in [
                    {"Origin": "null"},
                    {"Origin": "https://example.invalid"},
                    {"X-Hull-Control": "wrong"},
                    {"Sec-Fetch-Site": "cross-site"},
                ]:
                    self.assertEqual(req("/api/demo/check", b"{}", hd | bad)[0], 403)
                self.assertEqual(
                    req("/api/demo/check", b"{}", {**hd, "Content-Type": "text/plain"})[
                        0
                    ],
                    400,
                )
                self.assertEqual(req("/api/demo/check", b"{" * 300, hd)[0], 400)
                self.assertEqual(
                    req("/api/demo/check", b'{"symbols":["AAA"]}', hd)[0], 409
                )
                self.assertEqual(
                    req("/api/demo/check", b'{"command":"not accepted"}', hd)[0], 409
                )
                t = json.loads(req("/api/demo/check", b"{}", hd)[1])["ticket"]
                self.assertEqual(self.r.calls, 0)
                self.assertNotIn(t, req("/api/demo/status")[1].decode())
                self.assertEqual(
                    req(
                        "/api/demo/arm",
                        json.dumps({"ticket": t, "confirmation": "ARM DEMO"}).encode(),
                        hd,
                    )[0],
                    200,
                )
                self.assertEqual(self.r.calls, 1)
            finally:
                srv.shutdown()
                srv.server_close()
                thread.join()


class DesktopAdapterTests(unittest.TestCase):
    def test_fixed_argv_and_new_console_no_shell(self):
        from contextlib import ExitStack
        from tempfile import TemporaryDirectory

        import demo_control as mod

        with ExitStack() as c:
            root = Path(c.enter_context(TemporaryDirectory())) / "path with spaces"
            runner = mod.LocalRunner(root, 123)
            for p in (runner.py, runner.script):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.touch()
            c.enter_context(patch.object(mod, "os", NS(name="nt")))
            inventory = c.enter_context(patch.object(mod, "check_other_scanners"))
            c.enter_context(
                patch.object(mod.subprocess, "CREATE_NEW_CONSOLE", 16, create=True)
            )
            c.enter_context(
                patch.object(mod.subprocess, "CREATE_NO_WINDOW", 128, create=True)
            )
            run = c.enter_context(
                patch.object(
                    mod.subprocess,
                    "run",
                    return_value=NS(returncode=0, stdout=b'{"issues":[]}'),
                )
            )
            start = c.enter_context(patch.object(mod.subprocess, "Popen"))
            self.assertEqual(runner.review(WHO), {"issues": []})
            start.assert_not_called()
            digest = "f" * 64
            run_id = "a" * 32
            runner.start(WHO, digest, run_id, 222)
            for call, act, flags, tail in [
                (run.call_args, "check", 128, []),
                (start.call_args, "arm", 16, [digest, run_id, "222"]),
            ]:
                self.assertEqual(
                    call.args[0],
                    [
                        str(runner.py),
                        "-I",
                        "-B",
                        "-u",
                        str(runner.script),
                        act,
                        *WHO,
                        "123",
                        *tail,
                    ],
                )
                self.assertEqual(call.kwargs["cwd"], runner.root)
                self.assertEqual(call.kwargs["creationflags"], flags)
                self.assertFalse(call.kwargs["shell"])
            self.assertEqual(inventory.call_count, 2)
            inventory.side_effect = ControlError("scanner busy")
            with self.assertRaises(ControlError):
                runner.start(WHO, digest, run_id, 222)
            self.assertEqual(start.call_count, 1)
            # A receipt left by a different launch (or corrupted) is never a start signal.
            p = root / "reports/hull_runtime" / WHO[0] / "journal-control.json"
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps({"id": "b" * 32, "state": "started"}))
            self.assertFalse(runner.receipt(WHO, run_id))
            p.write_text("{")
            self.assertFalse(runner.receipt(WHO, run_id))

    def test_uninspectable_windows_inventory_blocks(self):
        import demo_control as mod

        with (
            patch.dict(mod.os.environ, {"SystemRoot": "Windows"}),
            patch.object(mod.subprocess, "CREATE_NO_WINDOW", 128, create=True),
            patch.object(mod.subprocess, "run") as call,
        ):
            for rc in (3, 9, 1):
                call.return_value = NS(returncode=rc)
                with self.assertRaises(ControlError):
                    mod.check_other_scanners()
            call.return_value = NS(returncode=0)
            mod.check_other_scanners()
            self.assertFalse(call.call_args.kwargs["shell"])

    def test_terminal_signature_includes_data_directory_and_normalizes_case(self):
        a = NS(path=r"C:\Terminal", data_path=r"C:\Users\T\AppData\MT5")
        b = NS(path=r"c:/Terminal/.", data_path=r"c:/USERS/t/AppData/MT5")
        self.assertEqual(terminal_key(a), terminal_key(b))
        b.data_path = b.path
        self.assertNotEqual(terminal_key(a), terminal_key(b))
        self.assertIsNone(terminal_key(NS(path=b.path)))
