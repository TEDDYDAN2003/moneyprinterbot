"""Hull Ledger • local, read-only journal companion. Run: python journal.py --help"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ledger import (
    MAGIC,
    JournalError,
    atomic,
    csv_bytes,
    db_open,
    get,
    make_views,
    sync,
)

HERE = Path(__file__).resolve().parent


@contextmanager
def single_writer(folder):
    # OS-owned lock is released even after a process crash; do not delete it to restart.
    f = (folder / "writer.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            if f.seek(0, 2) == 0:
                f.write(b"0")
                f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as ex:
        f.close()
        raise JournalError(
            "Another journal writer is using this output folder."
        ) from ex
    try:
        yield
    finally:
        f.close()


def utc():
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Desktop:
    def __init__(self):
        self.lock = threading.Lock()
        self.rows = {"tickets": [], "setups": []}
        self.rev = "initial"
        self.status = {
            "state": "waiting",
            "message": "Waiting for the first verified MT5 snapshot.",
            "last_sync": "",
            "counts": {},
        }

    def set(self, status, rows=None, rev=None):
        with self.lock:
            self.status = {**self.status, **status}
            if rows is not None:
                self.rows = rows
                self.rev = rev

    def snapshot(self, previous):
        with self.lock:
            data = {"revision": self.rev, "status": self.status}
            if previous != self.rev:
                data.update(self.rows)
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()


class Reporter:
    def __init__(self, conn, folder, desktop):
        self.db = conn
        self.folder = folder
        self.ui = desktop
        self.rebuild = True
        self.due = None
        self.hashes = {}
        self.counts = {}

    def poll(self, mt, now, **kw):
        poll_seconds = kw.pop("poll_seconds", 2)
        self.rebuild |= sync(self.db, mt, now, **kw)
        rows = None
        rev = None
        if (
            self.rebuild
            or self.due is not None
            and now >= self.due
            or any(
                not (self.folder / (k + ".csv")).is_file()
                for k in ("tickets", "setups")
            )
        ):
            self.rebuild = (
                True  # keep dirty across a locked CSV or interrupted publication
            )
            rows, counts, due = make_views(self.db)
            signatures = {}
            for k, rs in rows.items():
                p = self.folder / (k + ".csv")
                data = csv_bytes(rs)
                signatures[k] = hashlib.sha256(data).hexdigest()
                if self.hashes.get(k) != signatures[k] or not p.is_file():
                    atomic(p, data)
                    self.hashes[k] = signatures[k]
            self.hashes = signatures
            self.counts = counts
            self.due = due
            self.rebuild = False
            rev = ":".join(signatures.values())
        who = get(self.db, "identity")
        cur = get(self.db, "log_cursor", {})
        status = {
            "state": "live",
            "message": "Read-only MT5 history • exports synchronized",
            "last_sync": utc(),
            "account_key": who["key"],
            "currency": who["currency"],
            "counts": self.counts,
            "poll_seconds": poll_seconds,
            "currency_digits": who["digits"],
            "has_log": get(self.db, "has_log", False),
            "bad_log_lines": cur.get("bad", 0),
            "coverage_start": datetime.fromtimestamp(get(self.db, "floor"), UTC)
            .date()
            .isoformat(),
        }
        self.ui.set(status, rows, rev)
        return status


def make_server(ui, port, *, targets=None, magic=MAGIC):
    token = secrets.token_urlsafe(32) if targets is not None else ''
    template = (
        (HERE / "Hull-Journal.html")
        .read_text("utf8")
        .replace('data-feed="file"', f'data-feed="collector" data-target-key="{token}"', 1)
        .encode()
    )

    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def local_host(self):
            host = self.headers.get("Host", "").casefold()
            return (
                host
                if host
                in {
                    f"127.0.0.1:{self.server.server_port}",
                    f"localhost:{self.server.server_port}",
                }
                else None
            )

        def do_POST(self):
            if targets is None:
                return self.send_error(501)  # ordinary reporter has no controls
            host = self.local_host()
            if (
                not host
                or self.headers.get("Origin") != "http://" + host
                or not secrets.compare_digest(
                    self.headers.get("X-Hull-Target-Key", "").encode(), token.encode()
                )
            ):
                return self.send_error(403)
            if self.path not in ("/api/targets/save", "/api/targets/preflight"):
                return self.send_error(404)
            if (
                self.headers.get_content_type() != "application/json"
                or self.headers.get("Transfer-Encoding")
            ):
                return self.send_error(415)
            from bot.hull.target_settings import TargetSettingsError, unique

            try:
                n = int(self.headers.get("Content-Length", "0"))
                if not 0 < n <= 16384:
                    return self.send_error(413)
                self.connection.settimeout(10)
                body = json.loads(self.rfile.read(n), object_pairs_hook=unique)
                with ui.lock:
                    key = (
                        ui.status.get("account_key")
                        if ui.status["state"] == "live"
                        and 0
                        <= (
                            datetime.now(UTC)
                            - datetime.fromisoformat(ui.status["last_sync"])
                        ).total_seconds()
                        <= 30
                        else None
                    )
                result = targets.action(self.path.rsplit("/", 1)[1], body, key, magic)
                return self.reply(
                    json.dumps(result, allow_nan=False).encode(), "application/json"
                )
            except TargetSettingsError as exc:
                return self.reply(
                    json.dumps({"error": str(exc)}).encode(), "application/json", 409
                )
            except (
                ValueError,
                KeyError,
                TypeError,
                OSError,
                subprocess.SubprocessError,
            ):
                return self.reply(
                    b'{"error":"Settings or preflight unavailable. Check the private config locally."}',
                    "application/json",
                    400,
                )

        def do_GET(self):
            if not self.local_host():
                return self.send_error(403)
            u = urlsplit(self.path)
            if u.path in ("/", "/Hull-Journal.html"):
                body = template
                ct = "text/html; charset=utf-8"
            elif u.path == "/api/snapshot":
                body = ui.snapshot(parse_qs(u.query).get("since", [""])[0])
                ct = "application/json; charset=utf-8"
            elif u.path == "/api/targets" and targets is not None:
                try:
                    body = json.dumps(targets.profiles(), allow_nan=False).encode()
                except (ValueError, KeyError, OSError, TypeError):
                    return self.reply(
                        b'{"error":"Could not read private target settings. No files changed."}',
                        "application/json",
                        409,
                    )
                ct = "application/json; charset=utf-8"
            elif u.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            else:
                return self.send_error(404)
            return self.reply(body, ct)

        def reply(self, body, ct, code=200):
            self.send_response(code)
            self.send_header("Content-Type", ct)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    return ThreadingHTTPServer(("127.0.0.1", port), H)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--repo",
        type=Path,
        help="Hull bot folder; optional exact receipt/structure metadata (reads demo.jsonl only).",
    )
    p.add_argument(
        "--terminal-path",
        help="Full path to the intended terminal64.exe if several MT5 installations exist.",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=HERE / "data",
        help="Private local output directory; one account and magic per directory.",
    )
    p.add_argument(
        "--backfill-days",
        type=int,
        default=30,
        help="Initial/periodic history window (default: 30 days). Existing rows are retained.",
    )
    p.add_argument(
        "--poll-seconds",
        type=float,
        default=2,
        help="Delay after each poll; default 2 seconds.",
    )
    p.add_argument(
        "--magic", type=int, default=MAGIC, help="Hull entry magic; default 26111825."
    )
    p.add_argument("--target-config", type=Path, help="Opt in to local per-symbol target settings and read-only preflight. Full bot install and an existing private *.local.yaml required.")
    p.add_argument("--port", type=int, default=8765, help="Local-only dashboard port.")
    p.add_argument(
        "--open-browser",
        action="store_true",
        help="Open the local dashboard in your browser when the server starts.",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="One export, no server; nonzero exit on failure.",
    )
    p.add_argument(
        "--reconcile",
        action="store_true",
        help="On first poll also reread every tracked position for older broker corrections.",
    )
    args = p.parse_args(argv)
    if not (
        1 <= args.backfill_days <= 3650
        and 0.5 <= args.poll_seconds <= 300
        and 1 <= args.port <= 65535
        and 0 < args.magic < 2**31
    ):
        p.error("Invalid days (1–3650), interval (0.5–300s), port or magic.")
    targets = None
    if args.target_config:
        if args.once:
            p.error("--target-config needs the local dashboard, not --once")
        try:
            from target_panel import TargetPanel

            targets = TargetPanel(args.target_config)
            targets.profiles()  # reject unreadable/unknown settings, never replace them
        except (ImportError, OSError, ValueError, KeyError, TypeError):
            p.error(
                "Target editor needs the full bot environment and valid private settings. No files changed."
            )
    try:
        import MetaTrader5 as mt
    except ImportError:
        print(
            "Use your Windows MT5 Python environment, or install MetaTrader5 with that interpreter. No exports changed.",
            file=sys.stderr,
        )
        return 2
    args.out = args.out.expanduser().resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        with single_writer(args.out):
            cache = args.out / "journal.sqlite3"
            if not cache.exists() and any(
                (args.out / n).exists() for n in ("tickets.csv", "setups.csv")
            ):
                raise JournalError(
                    "This folder already contains CSVs without a journal cache. Use a new empty --out folder to avoid overwriting them."
                )
            c = db_open(cache)
            ui = Desktop()
            reporter = Reporter(c, args.out, ui)
            srv = None
            linked = False
            deep = args.reconcile
            previous = None
            try:
                if not args.once:
                    srv = make_server(ui, args.port, targets=targets, magic=args.magic)
                    threading.Thread(
                        target=srv.serve_forever, name="journal-dashboard", daemon=True
                    ).start()
                    print(
                        f"Dashboard: http://127.0.0.1:{args.port}/  |  Ctrl+C stops ONLY this reporter.",
                        flush=True,
                    )
                    if args.open_browser:
                        import webbrowser

                        webbrowser.open(f"http://127.0.0.1:{args.port}/")
                print("Read only • CSV location:", args.out, flush=True)
                while True:
                    try:
                        if not linked:
                            linked = bool(
                                mt.initialize(args.terminal_path)
                                if args.terminal_path
                                else mt.initialize()
                            )
                            if not linked:
                                raise JournalError(
                                    "MT5 connection unavailable. Check the signed-in terminal and --terminal-path."
                                )
                        status = reporter.poll(
                            mt,
                            time.time(),
                            magic=args.magic,
                            days=args.backfill_days,
                            repo=args.repo,
                            reconcile=deep,
                            poll_seconds=args.poll_seconds,
                        )
                        deep = False
                        report = f"{ui.status['currency']} · {len(ui.rows['tickets'])} closed tickets · {len(ui.rows['setups'])} complete setups · {status['counts']['open_tickets']} open · {status['counts']['review']} need review"
                        if report != previous:
                            print(utc(), report, flush=True)
                            previous = report
                        if args.once:
                            return 0
                    except (
                        JournalError,
                        OSError,
                        ValueError,
                        ArithmeticError,
                        sqlite3.Error,
                    ) as exc:
                        msg = (
                            "Close any spreadsheet locking the CSV and retry."
                            if isinstance(exc, PermissionError)
                            else str(exc)
                        )
                        ui.set({"state": "paused", "message": msg})
                        reporter.rebuild = True
                        if previous != msg:
                            print(
                                utc(), "Sync paused:", msg, file=sys.stderr, flush=True
                            )
                            previous = msg
                        if args.once:
                            return 1
                        # Re-initialize our own read-only IPC connection. Never log into or switch accounts.
                        if isinstance(exc, JournalError):
                            mt.shutdown()
                            linked = False
                    time.sleep(args.poll_seconds)
            finally:
                if srv:
                    srv.shutdown()
                    srv.server_close()
                mt.shutdown()
                c.close()
    except KeyboardInterrupt:
        print("\nJournal stopped. Broker positions and bot are untouched.")
        return 0
    except (JournalError, OSError, sqlite3.Error) as exc:
        print("Journal not started:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
