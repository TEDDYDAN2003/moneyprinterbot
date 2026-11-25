"""Opt-in, local desktop launch coordinator. No terminal API, arbitrary commands or stop/kill endpoint."""

from __future__ import annotations

import hashlib
import hmac
import json
import ntpath
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path

TTL = 120


class ControlError(ValueError):
    pass


def terminal_key(info):
    """Distinguish installations AND data directories (e.g. portable vs normal terminal)."""
    parts = [getattr(info, k, None) for k in ("path", "data_path")]
    if not all(isinstance(v, str) and ntpath.isabs(v) for v in parts):
        return None
    return hashlib.sha256(
        b"\0".join(ntpath.normcase(ntpath.normpath(v)).encode() for v in parts)
    ).hexdigest()


def check_other_scanners():
    # Fixed local process inventory. Do not interpolate HTTP arguments, print command lines,
    # stop an existing observer, or treat an inaccessible inventory as an empty one.
    ps = (
        Path(os.environ["SystemRoot"])
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    # Static query with no embedded double quotes, for Windows PowerShell's native argument parsing.
    query = r"""$ErrorActionPreference='Stop'; try {
    $filter='Name=''python.exe'' OR Name=''pythonw.exe'' OR Name=''dynamic-hull.exe'' OR Name=''xauusd-bot.exe''';
    $all=@(Get-CimInstance Win32_Process -Filter $filter);
    if(!$all.Count){exit 9};
    foreach($p in $all){ $s=$p.CommandLine; if([string]::IsNullOrWhiteSpace($s)){exit 9};
    if($s -match '(?i)(?:\bbot\.(?:cli|wickless)|(?:dynamic-hull|xauusd-bot)\.exe)\x22?\s+\x22?(?:trade-all|trade|demo|observe|reconcile)\b|demo_worker\.py\x22?\s+\x22?arm\b'){exit 3} }; exit 0
    } catch {exit 9}"""
    try:
        r = subprocess.run(
            [str(ps), "-NoProfile", "-NonInteractive", "-Command", query],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=12,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ControlError(
            "Cannot verify scanner inventory on this PC. No launch."
        ) from exc
    if r.returncode:
        raise ControlError(
            "Another scanner is running. Inspect it on the MT5 desktop; it was not stopped."
            if r.returncode == 3
            else "Scanner inventory is unavailable. No launch."
        )


class LocalRunner:
    def __init__(self, repo, magic):
        self.root, self.magic = Path(repo).resolve(), magic
        self.py = self.root / ".venv/Scripts/python.exe"
        self.script = self.root / "HullJournal/demo_worker.py"

    def argv(self, action, identity, *extra):
        if os.name != "nt" or not self.py.is_file() or not self.script.is_file():
            raise ControlError(
                "Use the full updated checkout and its .venv on the Windows MT5 PC."
            )
        return [
            str(self.py),
            "-I",
            "-B",
            "-u",
            str(self.script),
            action,
            *identity,
            str(self.magic),
            *extra,
        ]

    def review(self, identity):
        args = self.argv("check", identity)
        check_other_scanners()
        try:
            call = subprocess.run(
                args,
                cwd=self.root,
                shell=False,
                capture_output=True,
                check=False,
                stdin=subprocess.DEVNULL,
                timeout=300,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if call.returncode or len(call.stdout) > 256 * 1024:
                raise ValueError()
            result = json.loads(call.stdout.decode("utf-8"))
            if not isinstance(result, dict) or not isinstance(
                result.get("issues"), list
            ):
                raise TypeError()
            return result
        except (ValueError, TypeError, OSError, subprocess.SubprocessError) as exc:
            raise ControlError(
                "Readiness check failed or timed out. Check the venv and run the read-only desktop preflight for details."
            ) from exc

    def start(self, identity, digest, run_id, deadline):
        args = self.argv("arm", identity, digest, run_id, str(deadline))
        check_other_scanners()
        return subprocess.Popen(
            args,
            cwd=self.root,
            shell=False,
            close_fds=True,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )

    def receipt(self, identity, run_id):
        path = self.root / "reports/hull_runtime" / identity[0] / "journal-control.json"
        try:
            with path.open("rb") as f:
                r = json.loads(f.read(4097))
            return r if r.get("id") == run_id else {}
        except (OSError, ValueError, AttributeError):
            return {}


class DemoControl:
    """Fresh, single-use, server-owned reviews. Only the tab that requested a check gets its ticket."""

    def __init__(self, runner, identity, clock=time.monotonic):
        self.runner, self.identity, self.clock = runner, identity, clock
        self.key = secrets.token_urlsafe(
            32
        )  # embedded only in opt-in loopback HTML; never a query param
        self.mu = threading.Lock()
        self.op = threading.Lock()
        self.ticket = None
        self.report = None
        self.child = None
        self.active = None
        self.phase = "idle"
        self.note = "No scanner launched by this journal."

    def _update(self):
        if self.ticket and (
            self.clock() >= self.ticket["expires"]
            or self.identity() != self.ticket["identity"]
        ):
            self.ticket = None
            self.phase = "blocked"
            self.note = (
                "Review expired or journal account changed. Run preflight again."
            )
        if self.child is not None:
            rc = self.child.poll()
            receipt = self.runner.receipt(*self.active)
            if rc is not None:
                self.phase = "exited"
                self.note = (
                    receipt.get("reason")
                    or f"Scanner process exited ({rc}). Check MT5; no automatic restart."
                )
                self.child = None
            elif receipt.get("state") == "started":
                self.phase = "running"
                self.note = "Demo scanner confirmed startup. Closing this page does not stop it."
            else:
                self.phase = "starting"
                self.note = (
                    "Separate scanner window opened. Waiting for verified startup."
                )

    def status(self):
        with self.mu:
            self._update()
            return {
                "phase": self.phase,
                "message": self.note,
                "review": self.report,
                "pid": self.child.pid if self.child else None,
                "review_id": self.ticket["tag"] if self.ticket else None,
                "verified": self.identity() is not None,
                "remaining": max(0, int(self.ticket["expires"] - self.clock()))
                if self.ticket
                else 0,
            }

    def _who(self):
        who = self.identity()
        if (
            not who
            or not re.fullmatch("[a-f0-9]{16}", who[0])
            or not re.fullmatch("[a-f0-9]{64}", who[1])
        ):
            raise ControlError(
                "Wait for a fresh, connected USD demo journal snapshot on this terminal."
            )
        return who

    def action(self, action, data):
        if (
            not isinstance(data, dict)
            or action not in ("check", "arm")
            or set(data) != ({"ticket", "confirmation"} if action == "arm" else set())
        ):
            raise ControlError("Unexpected request fields.")
        if not self.op.acquire(False):
            raise ControlError("A readiness check or launch is already in progress.")
        try:
            with self.mu:
                self._update()
                if self.child:
                    raise ControlError(
                        "This journal already launched a scanner. No duplicate start."
                    )
                if action == "check":
                    self.ticket = self.report = None
                    self.phase = "checking"
                    self.note = (
                        "Checking the local configuration, terminal and bot tests…"
                    )
                else:
                    t = self.ticket
                    if (
                        not t
                        or not isinstance(data["ticket"], str)
                        or not data["ticket"].isascii()
                        or not hmac.compare_digest(t["id"], data["ticket"])
                        or data["confirmation"] != "ARM DEMO"
                    ):
                        raise ControlError(
                            "A matching review and explicit ARM DEMO confirmation are required."
                        )
                    self.ticket = None  # consume BEFORE any subprocess, including uncertain launches
                    self.phase = "checking"
                    self.note = "Rechecking before launch…"
            try:
                who = self._who()
                if action == "check":
                    r = self.runner.review(who)
                    digest = r.pop("digest", None)
                    if who != self._who():
                        raise ControlError("Journal identity changed during preflight.")
                    with self.mu:
                        self.report = r
                        if (
                            r["issues"]
                            or not isinstance(digest, str)
                            or not re.fullmatch("[a-f0-9]{64}", digest)
                        ):
                            self.phase = "blocked"
                            self.note = (
                                "Readiness checks did not pass. Nothing was armed."
                            )
                            return {"ok": False}
                        secret = secrets.token_urlsafe(24)
                        self.ticket = {
                            "id": secret,
                            "tag": secrets.token_hex(10),
                            "identity": who,
                            "digest": digest,
                            "expires": self.clock() + TTL,
                        }
                        self.phase = "ready"
                        self.note = "Review the exact profiles below before confirming."
                        return {
                            "ok": True,
                            "ticket": secret,
                            "review_id": self.ticket["tag"],
                        }
                if who != t["identity"] or self.clock() >= t["expires"]:
                    raise ControlError("Review expired or identity changed.")
                rid = secrets.token_hex(16)
                deadline = int(time.time() + t["expires"] - self.clock())
                child = self.runner.start(who, t["digest"], rid, deadline)
                with self.mu:
                    self.active = (who, rid)
                    self.child = child
                    self.phase = "starting"
                    self.note = "Scanner process launched; startup not yet confirmed."
                return {"ok": True}
            except Exception as exc:  # noqa: BLE001 - fail closed at the desktop boundary
                msg = (
                    str(exc)
                    if isinstance(exc, ControlError)
                    else "Desktop action failed. Inspect the Windows terminal; do not retry an uncertain launch."
                )
                with self.mu:
                    self.phase = "blocked"
                    self.note = msg
                    self.ticket = None
                raise ControlError(msg) from None
        finally:
            self.op.release()
