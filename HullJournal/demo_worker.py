"""Private fixed-action worker for the opt-in Windows journal. Never called by ordinary reporting."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# Python -I: use these trusted, operator-installed files, not a global copy, CWD or PYTHONPATH.
if __name__ == "__main__":
    sys.path[:0] = [str(ROOT / "src"), str(HERE)]

from demo_control import TTL, ControlError, terminal_key


def fingerprint(root, config):
    """Bind approval to the actual local bot, its tests, schedule, private file and resolved settings."""
    paths = [
        root / "pyproject.toml",
        root / "config.demo.local.yaml",
        root / "config.yaml",
        root / "HullJournal/demo_worker.py",
        root / "HullJournal/demo_control.py",
    ]
    for folder in ("src/bot", "tests"):
        paths.extend(
            p
            for p in sorted((root / folder).rglob("*"))
            if p.is_file()
            and p.suffix in {".py", ".yaml", ".json", ".cjs", ".js"}
            and "__pycache__" not in p.parts
        )
    h = hashlib.sha256()
    for p in paths:
        h.update(
            p.relative_to(root).as_posix().encode()
            + b"\0"
            + hashlib.sha256(p.read_bytes()).digest()
        )
    h.update(json.dumps(config, sort_keys=True, allow_nan=False).encode())
    return h.hexdigest()


def plan(summary, model, who, magic, mt, armed):
    from bot.hull.desk import identity_check

    a = identity_check(mt, "demo", armed)
    k = hashlib.sha256(repr((int(a.login), str(a.server))).encode()).hexdigest()[:16]
    tinfo = mt.terminal_info()
    h = model.hull
    issues = []
    rows = []
    notes = []
    if (
        summary.get("account_key") != who[0]
        or k != who[0]
        or terminal_key(tinfo) != who[1]
        or summary.get("journal_magic") != magic
        or h.magic != magic
        or summary.get("account") != "USD hedging demo"
    ):
        raise ControlError(
            "Preflight is not the journal’s exact terminal, demo account and magic. Check the private config."
        )
    if not (
        summary.get("account_expert_allowed") is True
        and summary.get("terminal_trade_allowed") is True
        and a.trade_expert
        and a.trade_allowed
        and getattr(tinfo, "trade_allowed", False)
        and getattr(tinfo, "connected", False)
        and getattr(tinfo, "tradeapi_disabled", None) is False
    ):
        issues.append("The demo account or terminal does not permit automated trading.")
    if summary.get("open_positions") != 0 or summary.get("pending_orders") != 0:
        issues.append(
            "The account is not verified flat: positions or pending orders exist."
        )
    if (
        summary.get("pair_history_ready") is not True
        or summary.get("execution_mode") != "paired_structure"
    ):
        issues.append("Paired bid-price history is not verified ready.")
    if not isinstance(summary.get("review_before_demo"), list):
        raise ControlError("Incomplete preflight; cannot review readiness.")
    for note in summary["review_before_demo"]:
        # Positive, operator-configured caps are informational; never tell someone to turn them off.
        (
            notes
            if note.startswith(
                "Optional Hull entry limits enabled in this config; confirm intended: "
            )
            else issues
        ).append(note)
    specs = summary["specs"]
    bykey = {s["key"]: (broker, s) for broker, s in specs.items()}
    if set(bykey) != set(h.enabled) or len(bykey) != len(specs):
        raise ControlError("Resolved selection does not match hull.enabled.")
    for k in h.enabled:
        broker, s = bykey[k]
        p = h.symbols[k]
        if (p.lots, p.target_usd, p.rate) != (s["lots"], s["target_usd"], s["rate"]):
            raise ControlError("Profile sizing changed. Review again.")
        rows.append(
            {
                "symbol": k,
                "broker": broker,
                "lots": p.lots,
                "target_usd": p.target_usd,
                "rate": p.rate,
                "legs": list(s["legs_when_room"]),
                "max_batch_lots": s["batch_lots_range"][-1],
            }
        )
    if not rows:
        raise ControlError("No enabled symbols.")
    return {
        "account": who[0],
        "account_hint": "…" + str(int(a.login))[-4:] + " on " + str(a.server)[:80],
        "magic": magic,
        "profiles": rows,
        "pairs": summary["parent_to_trigger"],
        "route": h.entry_route,
        "market_fallback": h.retest_market_fallback,
        "exit_policy": h.exit_policy,
        "positions": summary["open_positions"],
        "orders": summary["pending_orders"],
        "notes": notes,
        "issues": issues,
        "schedule": summary["entry_sessions"].get("sha256"),
        "revision": summary["checkout_head_at_start"],
    }


def safety_files(root, model, account, state=None):
    if (root / model.safety.emergency_flatten_hotkey_file).exists():
        raise ControlError("Emergency KILL is active. Do not start another scanner.")
    if state is None:
        p = Path.home() / ".moneyprinterbot/hull" / account / "state.json"
        state = json.loads(p.read_text("utf-8")) if p.exists() else {}
    if not isinstance(state, dict) or any(
        state.get(k)
        for k in (
            "manual_hold",
            "opposite_exits",
            "retest_batches_v1",
            "ladder_by_ticket",
            "target_by_ticket",
        )
    ):
        raise ControlError(
            "Saved account state needs its original manager or reconciliation. Do not clear or delete it."
        )


def tested(root):
    required = (
        "test_hull_paired_execution.py",
        "test_hull_direct_retest.py",
        "test_hull_journal_controls.py",
    )
    if not all((root / "tests" / f).is_file() for f in required):
        return False
    # A regression check can run only the repo's tests, in an isolated process with a
    # temporary home, its own basetemp, no third-party pytest plugins and NO terminal module.
    # The suites supply their own fabricated modules via monkeypatch, even on the MT5 PC.
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    for key in (
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "PYTEST_DEBUG",
        "PYTEST_CURRENT_TEST",
    ):
        env.pop(key, None)
    entry = "import sys; sys.modules['MetaTrader5']=None; import pytest; sys.exit(pytest.main(sys.argv[1:]))"
    try:
        with TemporaryDirectory(prefix="hull-offline-check-") as tmp:
            env.update(HOME=tmp, USERPROFILE=tmp)
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-X",
                    "utf8",
                    "-c",
                    entry,
                    "-q",
                    "-x",
                    "-p",
                    "no:cacheprovider",
                    "-o",
                    "pythonpath=src",
                    "-o",
                    "addopts=",
                    "--basetemp",
                    str(Path(tmp) / "pytest"),
                    "-c",
                    str(root / "pyproject.toml"),
                    "tests",
                ],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=180,
                shell=False,
            )
            return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def seal(build, review):
    return hashlib.sha256(
        json.dumps([build, review], sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def receipt(root, account, rid, state, reason=""):
    directory = root / "reports/hull_runtime" / account
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / "journal-control.json"
    t = directory / ("journal-" + rid + ".tmp")
    t.write_text(
        json.dumps({"id": rid, "state": state, "reason": reason}), encoding="utf-8"
    )
    os.replace(t, p)


def execute(root, action, who, magic, approval=None):
    from bot.broker import DemoSafetyError, snapshot
    from bot.config.loader import load_yaml
    from bot.config.schema import validate_config
    from bot.hull.config import selection
    from bot.hull.terminal import run

    if action not in {"check", "arm"} or (action == "arm") != (approval is not None):
        raise ControlError(
            "Expected a read-only check or an explicitly approved arm request."
        )
    if approval and not 0 < approval[2] - time.time() <= TTL + 1:
        raise ControlError("Approval expired before scanner startup.")
    private = root / "config.demo.local.yaml"
    if not private.is_file():
        raise ControlError(
            "Missing config.demo.local.yaml. The journal never falls back to config.yaml."
        )
    try:
        raw = load_yaml(private)
        model = validate_config(raw)
    except (ValueError, TypeError, KeyError) as exc:
        # Pydantic can echo config values. Only show safe local troubleshooting instructions.
        raise ControlError(
            "Invalid private configuration. On the MT5 PC use the checkout venv: python -m bot.cli validate-config --config config.demo.local.yaml. No scanner started."
        ) from exc
    h = model.hull
    if (
        model.run.mode != "demo"
        or h.execution_mode != "paired_structure"
        or h.magic != magic
    ):
        raise ControlError(
            "The private YAML must specify demo mode, paired_structure, and the journal’s magic."
        )
    private_hull = raw["hull"]
    if not private_hull.get("enabled") or any(
        not {"lots", "target_usd", "rate"} <= private_hull["symbols"][k].keys()
        for k in h.enabled
    ):
        raise ControlError(
            "Save hull.enabled and each selected profile’s lots, target_usd and rate explicitly in the private YAML. No template or sizing defaults are used."
        )
    cfg = model.model_dump() | {"hull": private_hull}
    profiles = selection(h)
    frames = h.lanes()
    safe_model = model.model_dump(mode="json")
    build = fingerprint(root, safe_model)
    safety_files(root, model, who[0])
    kwargs = {"cfg": cfg, "h": h, "profiles": profiles, "frames": frames}

    class Guard:
        def review(self, mt, summary):
            return plan(summary, model, who, magic, mt, armed=action == "arm")

        def verify(self, mt, summary, state):
            # Inside the actual runner, after both existing account locks, BEFORE Desk/recovery/order code.
            view = self.review(mt, summary)
            digest, _, deadline = approval
            ps, orders = snapshot(mt)
            if ps or orders:
                raise ControlError(
                    "Positions or pending orders appeared after review. Nothing armed."
                )
            safety_files(root, model, who[0], state)
            if (
                view["issues"]
                or fingerprint(root, safe_model) != build
                or seal(build, view) != digest
            ):
                raise ControlError(
                    "The reviewed source, settings, profiles or readiness changed. Run preflight again."
                )
            if not 0 < deadline - time.time() <= TTL + 1:
                raise ControlError("Approval expired before scanner startup.")

        def started(self):
            receipt(root, who[0], approval[1], "started")

    if action == "check":
        try:
            view = run(**kwargs, preflight=True, journal_guard=Guard())
        except DemoSafetyError as exc:
            raise ControlError(
                "MT5 preflight did not pass. Check terminal identity, permissions, broker symbol names and paired history. "
                + str(exc)[:400]
            ) from None
        if not view["issues"]:
            if not tested(root):
                view["issues"].append(
                    "Bot regression checks failed, are unavailable, or timed out. Run python -m pytest -q tests on this checkout. Arming is blocked."
                )
            elif fingerprint(root, safe_model) != build:
                view["issues"].append(
                    "Checkout or private configuration changed while checking. Run preflight again."
                )
        if not view["issues"]:
            view["digest"] = seal(build, view)
        return view

    run(**kwargs, armed=True, journal_guard=Guard())


def main(args):
    if os.name != "nt" or Path.cwd().resolve() != ROOT or len(args) not in (4, 7):
        return 2
    action, account, terminal, magic, *more = args
    if (
        (action, len(more)) not in {("check", 0), ("arm", 3)}
        or not re.fullmatch("[a-f0-9]{16}", account)
        or not re.fullmatch("[a-f0-9]{64}", terminal)
        or not magic.isdecimal()
    ):
        return 2
    if more and not (
        re.fullmatch("[a-f0-9]{64}", more[0])
        and re.fullmatch("[a-f0-9]{32}", more[1])
        and more[2].isdecimal()
    ):
        return 2
    try:
        out = execute(
            ROOT,
            action,
            (account, terminal),
            int(magic),
            (more[0], more[1], int(more[2])) if more else None,
        )
        if action == "check":
            print(json.dumps(out, ensure_ascii=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - do not leak private validation input
        # Never publish raw YAML validation errors (they may quote private values), credentials or logs.
        reason = (
            str(exc)
            if isinstance(exc, ControlError)
            else "Local validation or MT5 preflight failed. Check the private YAML and the venv on the MT5 desktop."
            if action == "check"
            else "Scanner process stopped. Inspect the MT5 positions, pending orders, scanner log and locks on the desktop before any restart."
        )
        if action == "check":
            print(json.dumps({"issues": [reason]}, ensure_ascii=True))
            return 0
        receipt(ROOT, account, more[1], "exited", reason)
        print(reason, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
