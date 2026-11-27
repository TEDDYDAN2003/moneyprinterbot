"""The primary CLI/config are Hull-only and preserve private clock/limit settings."""

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.cli import main
from bot.config.loader import load_yaml
from bot.config.schema import validate_config
from bot.hull.hours import Hours

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


def test_template_is_for_the_actual_demo_desk():
    raw = load_yaml(CONFIG)
    conf = validate_config(raw)
    assert set(raw) == {"run", "sessions", "safety", "mt5", "hull"}
    assert (conf.run.mode, conf.hull.magic, conf.hull.lanes(), conf.hull.enabled) == (
        "paper",
        26111825,
        (5, 10, 15),
        ("XAUUSD",),
    )
    assert (
        conf.hull.effective_timing == "intrabar_cross"
        and conf.hull.indicator.stop == "cross_candle"
    )
    assert validate_config({"hull": raw["hull"]}).run.mode == "paper"
    with pytest.raises(ValueError, match="hull"):
        validate_config({"run": {}, "zones": {}, "data": {}})


@pytest.mark.parametrize("mode", ["auto", "live", "backtest", "contest"])
def test_unsafe_or_retired_mode_rejected_before_terminal(mode):
    with pytest.raises(ValueError, match="run.mode"):
        validate_config(load_yaml(CONFIG) | {"run": {"mode": mode}})


def test_old_private_hours_caps_and_magic_are_not_lost():
    raw = load_yaml(CONFIG)
    raw["run"]["mode"] = "demo"
    raw["run"]["market_close_timezone"] = "UTC"
    del raw["sessions"]["maintenance_start"], raw["sessions"]["maintenance_end"]
    raw["sessions"]["trade_only_in"] = ["ny_open"]
    raw["pilot"] = {
        "daily_flatten_time": "14:00",
        "daily_reopen_time": "14:30",
        "max_hold_m1_bars": 1,
    }
    raw["hull"]["magic"] = 98765
    raw["hull"]["limits"]["max_open_risk_usd"] = 45
    cfg = validate_config(raw).model_dump()
    assert (
        "pilot" not in cfg
        and cfg["hull"]["limits"]["max_open_risk_usd"] == 45
        and cfg["hull"]["magic"] == 98765
    )
    assert (
        cfg["sessions"]["maintenance_start"] == "14:00"
        and cfg["sessions"]["maintenance_end"] == "14:30"
    )
    at = lambda hour, minute: datetime(
        2026, 11, 17, hour, minute, tzinfo=UTC
    ).timestamp()
    for config in (raw, cfg):
        assert Hours(config).status(at(14, 2))[0] == "scheduled_break"
        assert Hours(config).status(at(14, 30))[0] == ""
        assert Hours(config).status(at(19, 0))[0] == "outside_session"
    raw["sessions"]["maintenance_start"] = "15:00"
    with pytest.raises(ValueError, match="Conflicting"):
        validate_config(raw)


@pytest.mark.parametrize(
    "cmd",
    [
        "train",
        "label",
        "features",
        "download",
        "zones-replay",
        "candle-test",
        "hull-replay",
    ],
)
def test_retired_commands_never_fall_back_or_initialize(cmd, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "MetaTrader5",
        SimpleNamespace(initialize=lambda: pytest.fail("must be offline")),
    )
    with pytest.raises(SystemExit) as e:
        main([cmd])
    assert e.value.code == 2


@pytest.mark.parametrize("cmd", ["trade-all", "trade", "demo", "hull-preflight"])
def test_retained_desktop_commands_route_only_to_dynamic_scanner(cmd, monkeypatch):
    calls = []
    monkeypatch.setattr("bot.hull.terminal.run", lambda *a, **kw: calls.append((a, kw)))
    assert (
        main(
            [
                cmd,
                "--config",
                str(CONFIG),
                "--timeframe",
                "M10",
                "--profile",
                "XAUUSD:0.09:10:slow",
            ]
        )
        == 0
    )
    ((args, kw),) = calls
    cfg, h, selection, lanes = args
    assert (
        cfg["run"]["mode"] == "paper"
        and h.indicator.stop == "cross_candle"
        and h.effective_timing == "intrabar_cross"
    )
    assert (lanes, tuple(selection), selection["XAUUSD"].lots) == (
        (10,),
        ("XAUUSD",),
        0.09,
    )
    assert not kw["armed"] and kw["preflight"] == (cmd == "hull-preflight")
