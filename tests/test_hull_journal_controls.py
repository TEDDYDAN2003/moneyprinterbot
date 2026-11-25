"""Verify the opt-in journal worker against a fabricated terminal. No real MT5 or subprocess launches."""

import hashlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from hull_mt import MT, ROOT, T

from bot.config.loader import load_yaml
from bot.hull import terminal

sys.path.insert(0, str(ROOT / "HullJournal"))
import demo_worker as w
from demo_control import ControlError, terminal_key


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    import yaml

    raw = load_yaml(ROOT / "config.yaml")
    raw["run"]["mode"] = "demo"
    raw["hull"]["enabled"] = ["GBPUSD", "XAUUSD"]
    raw["hull"]["symbols"]["GBPUSD"]["lots"] = 0.38
    cfg = tmp_path / "config.demo.local.yaml"
    cfg.write_text(yaml.safe_dump(raw))
    mt = MT()
    mt.a.path = r"C:\Desk MT5"
    mt.a.data_path = r"C:\Users\tester\MT5data"
    mt.a.tradeapi_disabled = False
    who = (
        hashlib.sha256(repr((int(mt.a.login), str(mt.a.server))).encode()).hexdigest()[
            :16
        ],
        terminal_key(mt.a),
    )
    magic = raw["hull"]["magic"]
    data = {
        "warning": None,
        "tested": True,
        "started": 0,
        "build": "stable",
        "race": None,
    }

    def run(**kw):
        h = kw["h"]
        names = [(k, k + "m") for k in h.enabled]
        assert list(kw["profiles"]) == list(h.enabled)
        s = {
            "account_key": who[0],
            "journal_magic": magic,
            "account": "USD hedging demo",
            "specs": {
                br: {
                    "key": k,
                    "legs_when_room": (10,),
                    "batch_lots_range": [10 * h.symbols[k].lots] * 2,
                    **h.symbols[k].model_dump(),
                }
                for k, br in names
            },
            "execution_mode": "paired_structure",
            "account_expert_allowed": True,
            "terminal_trade_allowed": True,
            "pair_history_ready": True,
            "open_positions": len(mt.positions),
            "pending_orders": len(mt.orders),
            "review_before_demo": [data["warning"]] if data["warning"] else [],
            "parent_to_trigger": {"M5": "M4"},
            "checkout_head_at_start": "test",
            "entry_sessions": {"sha256": "sample"},
        }
        if kw.get("preflight"):
            assert not kw.get("armed")
            return kw["journal_guard"].review(mt, s)
        assert kw["armed"]
        guard = kw["journal_guard"]
        if data["race"]:
            data["race"]()
        guard.verify(mt, s, {})
        guard.started()
        data["started"] += 1

    monkeypatch.setattr(terminal, "run", run)
    monkeypatch.setattr(w, "tested", lambda _: data["tested"])
    monkeypatch.setattr(
        w, "fingerprint", lambda root, m: data["build"] + json.dumps(m, sort_keys=True)
    )

    def check():
        return w.execute(tmp_path, "check", who, magic)

    def arm(r, delta=90):
        return w.execute(
            tmp_path,
            "arm",
            who,
            magic,
            (r["digest"], "a" * 32, int(time.time()) + delta),
        )

    return NS(
        root=tmp_path,
        check=check,
        arm=arm,
        mt=mt,
        who=who,
        magic=magic,
        raw=raw,
        cfg=cfg,
        state=data,
    )


def test_only_enabled_yaml_and_receipt_after_guard(desk):
    r = desk.check()
    assert [v["symbol"] for v in r["profiles"]] == ["GBPUSD", "XAUUSD"]
    assert r["profiles"][0]["lots"] == 0.38
    assert desk.state["started"] == 0
    desk.arm(r)
    assert desk.state["started"] == 1
    receipt = json.loads(next(desk.root.rglob("journal-control.json")).read_text())
    assert receipt["state"] == "started"
    assert not desk.mt.requests


@pytest.mark.parametrize(
    "case",
    [
        "expiry",
        "account",
        "term",
        "data_dir",
        "permissions",
        "position",
        "order",
        "inventory_unknown",
        "kill",
        "changed_source",
        "changed_config",
        "warnings",
        "hold",
    ],
)
def test_arm_rechecks_state_without_orders(desk, case):
    d = desk
    r = d.check()
    if case == "account":
        d.state["race"] = lambda: setattr(d.mt.a, "login", 999)
    if case == "term":
        d.state["race"] = lambda: setattr(d.mt.a, "path", r"c:\other")
    if case == "data_dir":
        d.mt.a.data_path = r"C:\another terminal profile"
    if case == "permissions":
        d.mt.a.tradeapi_disabled = True
    if case == "position":
        d.state["race"] = lambda: d.mt.positions.append(object())
    if case == "order":
        d.state["race"] = lambda: d.mt.orders.append(object())
    if case == "inventory_unknown":
        d.mt.positions_get = lambda: None
    if case == "kill":
        d.state["race"] = lambda: (d.root / "KILL").touch()
    if case == "changed_source":
        d.state["build"] = "new"
    if case == "changed_config":
        d.cfg.write_text(d.cfg.read_text().replace("0.38", "0.39"))
    if case == "warnings":
        d.state["warning"] = "new review warning"
    if case == "hold":
        p = Path.home() / ".moneyprinterbot/hull" / d.who[0] / "state.json"
        p.parent.mkdir(parents=True)
        p.write_text('{"retest_batches_v1":{"hold":true}}')
    with pytest.raises(RuntimeError if case == "inventory_unknown" else ControlError):
        d.arm(r, -1 if case == "expiry" else 90)
    assert d.state["started"] == 0 and not d.mt.requests


@pytest.mark.parametrize(
    "problem",
    [
        "suite",
        "warning",
        "exposure",
        "pending",
        "wrongyaml",
        "missingyaml",
        "wrongmagic",
    ],
)
def test_preflight_never_returns_approval_for_blockers(desk, problem):
    if problem == "wrongyaml":
        desk.cfg.write_text(desk.cfg.read_text().replace("mode: demo", "mode: paper"))
    if problem == "missingyaml":
        desk.cfg.unlink()
    if problem == "wrongmagic":
        desk.cfg.write_text(desk.cfg.read_text().replace(str(desk.magic), "555"))
    if problem in {"missingyaml", "wrongyaml", "wrongmagic"}:
        with pytest.raises(ControlError):
            desk.check()
    else:
        if problem == "suite":
            desk.state["tested"] = False
        if problem == "warning":
            desk.state["warning"] = "not ready"
        if problem == "exposure":
            desk.mt.positions.append(object())
        if problem == "pending":
            desk.mt.orders.append(object())
        v = desk.check()
        assert v["issues"] and "digest" not in v
    assert desk.state["started"] == 0 and not desk.mt.requests


def test_real_runner_calls_guard_before_any_desk_side_effect(monkeypatch, tmp_path):
    from bot.hull.config import HullConfig, selection

    raw = load_yaml(ROOT / "config.yaml")
    raw["run"]["mode"] = "demo"
    raw["hull"]["eligible_timeframes"] = ["M5"]
    h = HullConfig.model_validate(raw["hull"])
    mt = MT()
    mt.TIMEFRAME_M4 = 4
    mt.initialize = lambda *a: True
    mt.shutdown = lambda: None
    mt.a.path = r"C:\MT5"
    mt.a.data_path = r"C:\Users\tester\MT5data"
    mt.a.tradeapi_disabled = False

    def rates(n, tf, pos, count):
        step = tf * 60
        start = T - T % step - (pos + count - 1) * step
        return [
            {
                "time": start + i * step,
                "open": 2000.0,
                "high": 2000.1,
                "low": 1999.9,
                "close": 2000.0,
            }
            for i in range(count)
        ]

    mt.copy_rates_from_pos = rates
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(terminal, "time", NS(time=lambda: T + 0.1))
    monkeypatch.setattr(
        terminal,
        "Desk",
        lambda *a, **k: pytest.fail("Desk was created before rejected review"),
    )

    class Guard:
        def verify(self, api, s, state):
            assert (
                api is mt
                and s["pair_history_ready"]
                and s["parent_to_trigger"] == {"M5": "M4"}
                and s["open_positions"] == s["pending_orders"] == 0
            )
            assert list(tmp_path.rglob("scanner.lock"))
            raise ControlError("test denial")

    with pytest.raises(ControlError, match="test denial"):
        terminal.run(
            raw, h, selection(h), armed=True, journal_guard=Guard(), iterations=1
        )
    assert not mt.requests and not list(tmp_path.rglob("state.json"))


@pytest.mark.parametrize("missing", ["enabled", "target_usd", "rate", "lots"])
def test_no_implicit_symbol_or_sizing_defaults(desk, missing):
    import yaml

    raw = desk.raw
    if missing == "enabled":
        del raw["hull"]["enabled"]
    else:
        del raw["hull"]["symbols"]["GBPUSD"][missing]
    desk.cfg.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError):
        desk.check()
    assert not desk.state["started"]


def test_fingerprint_binds_the_actual_files_and_resolved_model(tmp_path):
    paths = (
        "pyproject.toml",
        "config.yaml",
        "config.demo.local.yaml",
        "HullJournal/demo_worker.py",
        "HullJournal/demo_control.py",
        "src/bot/cli.py",
        "src/bot/config/entry_sessions.yaml",
        "tests/test_some_guard.py",
    )
    for name in paths:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"original")
    d = w.fingerprint(tmp_path, {"k": 1})
    assert w.fingerprint(tmp_path, {"k": 2}) != d
    for name in paths:
        p = tmp_path / name
        p.write_bytes(b"changed")
        assert w.fingerprint(tmp_path, {"k": 1}) != d
        p.write_bytes(b"original")
    assert w.fingerprint(tmp_path, {"k": 1}) == d


def test_offline_regression_gate_fail_closed_and_uses_no_mt5(tmp_path, monkeypatch):
    import subprocess

    assert not w.tested(tmp_path)
    (tmp_path / "tests").mkdir()
    for f in (
        "test_hull_paired_execution.py",
        "test_hull_direct_retest.py",
        "test_hull_journal_controls.py",
    ):
        (tmp_path / "tests" / f).touch()
    rc = 1

    def fake(cmd, **kw):
        assert (
            cmd[0] == sys.executable
            and "-I" in cmd
            and cmd[-1] == "tests"
            and "sys.modules['MetaTrader5']=None" in cmd[cmd.index("-c") + 1]
        )
        assert (
            kw["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
            and "PYTEST_ADDOPTS" not in kw["env"]
            and kw["env"]["USERPROFILE"] != str(Path.home())
            and not kw["shell"]
        )
        return NS(returncode=rc)

    monkeypatch.setenv("PYTEST_ADDOPTS", "-k pretend")
    monkeypatch.setattr(subprocess, "run", fake)
    assert not w.tested(tmp_path)
    rc = 0
    assert w.tested(tmp_path)

    def too_slow(*a, **kw):
        raise subprocess.TimeoutExpired("offline fake", 180)

    monkeypatch.setattr(subprocess, "run", too_slow)
    assert not w.tested(tmp_path)


def test_end_to_end_worker_with_real_runner_and_fabricated_mt5(tmp_path, monkeypatch):
    """Real warm-up, locks, stable review and 'started' audit; the terminal cannot send or connect."""
    from functools import partial
    from shutil import copyfile

    import yaml

    root = tmp_path / "desk"
    for rel in (
        "config.yaml",
        "pyproject.toml",
        "HullJournal/demo_worker.py",
        "HullJournal/demo_control.py",
        "src/bot/hull/terminal.py",
        "src/bot/config/entry_sessions.yaml",
    ):
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        copyfile(ROOT / rel, dest)
    raw = load_yaml(root / "config.yaml")
    h = raw["hull"]
    raw["run"]["mode"] = "demo"
    h["eligible_timeframes"] = ["M5"]
    h["enabled"] = ["XAUUSD"]
    h["symbols"]["XAUUSD"].update(lots=0.18, target_usd=19, rate="medium")
    (root / "config.demo.local.yaml").write_text(yaml.safe_dump(raw))
    monkeypatch.chdir(root)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    mt = MT()
    mt.TIMEFRAME_M4 = 4
    mt.a.path = r"C:\Synthetic MT5"
    mt.a.data_path = r"C:\Synthetic data"
    mt.a.tradeapi_disabled = False
    mt.initialize = lambda *a: True
    mt.shutdown = lambda: None

    def rates(name, tf, pos, count):
        step = tf * 60
        start = T - T % step - (pos + count - 1) * step
        return [
            {
                "time": start + i * step,
                "open": 2000.0,
                "high": 2000.1,
                "low": 1999.9,
                "close": 2000.0,
            }
            for i in range(count)
        ]

    mt.copy_rates_from_pos = rates
    mt.order_send = lambda *_: pytest.fail(
        "Fabricated desk refuses all order operations"
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt)
    monkeypatch.setattr(
        terminal, "time", NS(time=lambda: T + 0.1, monotonic=time.monotonic)
    )
    monkeypatch.setattr(terminal, "run", partial(terminal.run, iterations=1))
    # This test is about the integration, not overriding any red checks on a real checkout.
    monkeypatch.setattr(w, "tested", lambda p: p == root)
    who = (
        hashlib.sha256(repr((mt.a.login, mt.a.server)).encode()).hexdigest()[:16],
        terminal_key(mt.a),
    )
    review = w.execute(root, "check", who, h["magic"])
    assert not review["issues"] and not (root / "reports").exists()
    assert review["pairs"] == {"M5": "M4"} and review["profiles"] == [
        {
            "symbol": "XAUUSD",
            "broker": "XAUUSDm",
            "lots": 0.18,
            "target_usd": 19,
            "rate": "medium",
            "legs": [4, 3],
            "max_batch_lots": 0.72,
        }
    ]
    rid = "b" * 32
    w.execute(
        root, "arm", who, h["magic"], (review["digest"], rid, int(time.time()) + 100)
    )
    logs = root / "reports/hull_runtime" / who[0]
    assert (
        json.loads((logs / "journal-control.json").read_text())["id"] == rid
        and json.loads((logs / "journal-control.json").read_text())["state"]
        == "started"
    )
    rows = [json.loads(x) for x in (logs / "demo.jsonl").read_text().splitlines()]
    assert rows[0]["event"] == "started"
    assert rows[0]["summary"]["specs"]["XAUUSDm"]["lots"] == 0.18
    assert not mt.requests
