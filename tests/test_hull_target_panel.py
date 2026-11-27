"""Journal -> private preset -> real CLI selection/preflight on fake MT5. No scanner subprocess."""

import hashlib
import io
import json
import sys
import threading
from contextlib import redirect_stdout
from types import SimpleNamespace as NS
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import yaml
from hull_mt import ROOT
from test_hull_intrabar_market import NAME, OFF, ON, Tape

from bot.cli import main
from bot.config.loader import load_yaml
from bot.hull.config import HullConfig, selection
from bot.hull.target_settings import TargetSettingsError, effective_config, sidecar


@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "HullJournal"))
    import target_panel

    tape = Tape(tmp_path, monkeypatch, [(100, OFF), (200, ON)])
    cfg = tape.cfg
    cfg["hull"]["enabled"] = ["XAUUSD", "EURUSD"]
    cfg["hull"]["symbols"]["XAUUSD"].update(target_usd=12.5, rate="medium")
    cfg["hull"]["symbols"]["EURUSD"].update(target_usd=4.75, max_spread=0.3)
    path = tmp_path / "config.demo.local.yaml"
    path.write_text(
        "# keep private YAML bytes, including comments\n" + yaml.safe_dump(cfg)
    )
    tape.mt.catalog.append(NS(name="EURUSDm"))
    return target_panel.TargetPanel(path), tape


def test_two_custom_bases_survive_reload_change_one_and_reprice_at_actual_spread(
    panel, monkeypatch
):
    import target_panel

    api, tape = panel
    original = api.config.read_bytes()
    assert not sidecar(api.config).exists()
    snap = api.profiles()
    assert [(x["symbol"], x["effective_base"]) for x in snap["profiles"]] == [
        ("XAUUSD", 12.5),
        ("EURUSD", 4.75),
    ]
    magic = tape.cfg["hull"]["magic"]
    key = hashlib.sha256(repr((1, "t")).encode()).hexdigest()[:16]
    snap = api.action(
        "save",
        {"revision": snap["revision"], "changes": {"XAUUSD": 17.25, "EURUSD": 8}},
        None,
        magic,
    )
    again = target_panel.TargetPanel(api.config)
    assert again.profiles() == snap
    snap = again.action(
        "save", {"revision": snap["revision"], "changes": {"EURUSD": 7}}, None, magic
    )

    def run(argv, **kw):
        assert argv[:4] == [sys.executable, "-m", "bot.cli", "hull-preflight"] and argv[
            4:
        ] == ["--config", str(api.config), "--paired-confirm"]
        assert kw["cwd"] == api.config.parent and not kw.get("shell")
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert main(argv[3:]) == 0
        return NS(returncode=0, stdout=buf.getvalue())

    monkeypatch.setattr(target_panel, "subprocess", NS(run=run, DEVNULL=-3))
    raw_cash = tape.mt.order_calc_profit
    tape.mt.order_calc_profit = lambda typ, *rest: (
        raw_cash(typ, *rest) * (2 if typ else 1)
    )
    for width in (0.05, 0.1):

        def quote(name, width=width):
            q = tape.tick(name)
            q.ask = round(q.bid + width, 3)
            return q

        tape.mt.symbol_info_tick = quote
        rows = again.action("preflight", {"revision": snap["revision"]}, key, magic)[
            "profiles"
        ]
        assert [(r["broker"], r["effective_base"]) for r in rows] == [
            (NAME, 17.25),
            ("EURUSDm", 7.0),
        ]
        for row in rows:
            for side, factor in [("BUY", 1), ("SELL", 2)]:
                preview = row["preview"][side]
                spread = width * row["lots"] * 100 * factor
                assert preview["status"] == "indicative"
                assert preview["entry_spread_usd"] == pytest.approx(spread)
                assert preview["initial_goal_usd"] == pytest.approx(
                    row["effective_base"] + spread
                )
    # CLI explicit overrides are per run; no loss of the persisted bases or selection.
    saved = HullConfig.model_validate(
        effective_config(api.config, load_yaml(api.config))["hull"]
    )
    assert list(selection(saved)) == ["XAUUSD", "EURUSD"]
    assert selection(saved, profiles=["XAUUSD:0.2:33:fast"])["XAUUSD"].target_usd == 33
    assert selection(saved)["XAUUSD"].target_usd == 17.25
    restored = again.action(
        "save", {"revision": snap["revision"], "changes": {"XAUUSD": None}}, None, magic
    )
    assert [r["effective_base"] for r in restored["profiles"]] == [12.5, 7]
    assert not tape.mt.requests and api.config.read_bytes() == original
    for launcher in ("run_preflight.ps1", "run_arm_demo.ps1"):
        text = (ROOT / launcher).read_text()
        assert (
            "--config $cfg" in text
            and "--profile" not in text
            and "--symbols" not in text
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "negative",
        "boolean",
        "nan",
        "stale",
        "old_format",
        "repeat",
        "inactive",
        "wrong_magic",
    ],
)
def test_bad_or_stale_edit_never_resets_existing_profile(panel, mutation):
    api, tape = panel
    rev = api.profiles()["revision"]
    magic = tape.cfg["hull"]["magic"]
    old = api.action("save", {"revision": rev, "changes": {"EURUSD": 13}}, None, magic)
    rev = rev if mutation == "stale" else old["revision"]
    changes = {"XAUUSD": 22}
    if mutation in ("unknown", "inactive"):
        changes = {("bogus" if mutation == "unknown" else "AUDUSD"): 12}
    if mutation in ("negative", "boolean", "nan"):
        changes["XAUUSD"] = {"negative": -1, "boolean": True, "nan": float("nan")}[
            mutation
        ]
    file = sidecar(api.config)
    if mutation == "old_format":
        file.write_text('{"version":42,"targets":{}}')
    if mutation == "repeat":
        file.write_text('{"version":1,"targets":{"XAUUSD":11,"XAUUSD":12}}')
    original = file.read_bytes()
    with pytest.raises(TargetSettingsError):
        api.action(
            "save",
            {"revision": rev, "changes": changes},
            None,
            magic + int(mutation == "wrong_magic"),
        )
    assert file.read_bytes() == original and not tape.mt.requests


def test_read_only_preflight_failure_no_old_goals_or_mutation(panel, monkeypatch):
    import target_panel

    api, tape = panel
    rev = api.profiles()["revision"]

    def run(*args, **kwargs):
        return NS(returncode=1, stdout="private diagnostic not for HTTP")

    monkeypatch.setattr(target_panel, "subprocess", NS(run=run, DEVNULL=-3))
    with pytest.raises(TargetSettingsError, match="Preflight blocked"):
        api.action("preflight", {"revision": rev}, "account", tape.cfg["hull"]["magic"])
    assert not sidecar(api.config).exists() and all(
        "preview" not in x for x in api.profiles()["profiles"]
    )


def test_settings_are_opt_in_same_origin_and_only_have_two_actions(panel):
    from journal import Desktop, make_server

    api, _ = panel
    import re

    for active in (False, True):
        srv = make_server(Desktop(), 0, targets=api if active else None)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        base = f"http://127.0.0.1:{srv.server_port}"
        try:
            with urlopen(base) as r:
                token = re.search(r'data-target-key="([^"]*)"', r.read().decode())[1]
            assert bool(token) == active
            if not active:
                with pytest.raises(HTTPError) as err:
                    urlopen(base + "/api/targets")
                assert err.value.code == 404
            body = json.dumps(
                {"revision": api.profiles()["revision"], "changes": {"XAUUSD": 16.5}}
            ).encode()
            headers = {
                "Origin": base,
                "Content-Type": "application/json",
                "X-Hull-Target-Key": token,
            }
            for path, h, expected in [
                ("save", {"Origin": "http://example.invalid"}, 403),
                ("save", {"X-Hull-Target-Key": ""}, 403),
                ("start", {}, 404),
            ]:
                with pytest.raises(HTTPError) as err:
                    urlopen(
                        Request(
                            base + "/api/targets/" + path,
                            data=body,
                            headers=headers | h,
                        )
                    )
                assert err.value.code == (expected if active else 501)
            if active:
                with urlopen(
                    Request(base + "/api/targets/save", data=body, headers=headers)
                ) as r:
                    assert json.load(r)["profiles"][0]["effective_base"] == 16.5
            else:
                assert not sidecar(api.config).exists()
        finally:
            srv.shutdown()
            srv.server_close()
            th.join()
