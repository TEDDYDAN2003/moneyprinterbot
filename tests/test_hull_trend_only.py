"""End-to-end negative retest and positive trend-edge regression. Mock MT5 only."""

import json
import sys
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_cases import PATH, sequence
from hull_mt import MT, ROOT, T, scanner_clock, sig
from pydantic import ValidationError

from bot.config.loader import load_yaml
from bot.hull.config import CLOSE_ONLY, RETIRED_LIVE_TIMING, HullConfig, selection
from bot.hull.terminal import run
from bot.hull.structure import ENTRY_POLICY


@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize("tf", [1, 2, 5])
def test_former_watch_strict_close_and_exact_later_touch_never_reach_desk(
    tmp_path, monkeypatch, tf, side
):
    # The same three real Hull events (not injected readings) as the v3 regression.
    # Start before the old watch; stay connected across all intermediate closes.
    data = json.loads((ROOT / "MT5/qa/candles.json").read_text())["fixtures"]["base"][
        :421
    ]
    data[418][3] = 92.198
    data[420][3] = 92.281
    if side == -1:
        x = lambda p: round(200 - p, 3)
        data = [[t, x(o), x(l), x(h), x(c)] for t, o, h, l, c in data]
    dt = tf * 60
    rates = [
        dict(time=T + (i - 410) * dt, open=o, high=h, low=l, close=c)
        for i, (_, o, h, l, c) in enumerate(data)
    ]
    mt = MT()
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["run"]["mode"] = "demo"
    cfg["hull"]["symbols"]["XAUUSD"]["session"] = "continuous"
    cfg["hull"]["entry_timing"] = (
        RETIRED_LIVE_TIMING  # older saved local config must also be safe
    )
    h = HullConfig.model_validate(cfg["hull"])
    n = [410]

    def wall():
        return rates[n[0] - 1]["time"] + dt + 0.2

    def get(name, frame, shift, count):
        assert (name, frame, shift) == ("XAUUSDm", tf, 1)
        return rates[max(0, n[0] - count) : n[0]]

    def tick(_):
        px = rates[n[0] - 1]["close"]
        return NS(time_msc=int(wall() * 1000) - 10, bid=px, ask=round(px + 0.001, 3))

    def forbidden(*_):
        raise AssertionError("A former reversal setup reached a broker order API")

    mt.copy_rates_from_pos = get
    mt.symbol_info_tick = tick
    mt.order_check = mt.order_send = forbidden
    mt.initialize = lambda: True
    mt.shutdown = lambda: None
    scanner_clock(monkeypatch, wall, lambda _: n.__setitem__(0, n[0] + 1))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt)
    run(cfg, h, selection(h), (tf,), armed=True, iterations=12)
    log = [
        json.loads(x)
        for x in next(tmp_path.glob("reports/hull_runtime/*/demo.jsonl"))
        .read_text()
        .splitlines()
    ]
    summary = log[0]["summary"]
    assert (
        summary["entry_policy"] == ENTRY_POLICY
        and summary["choch_entry"] == "none"
        and summary["retired_clock_migrated"]
    )

    def at(i):
        return rates[i]["time"] + dt

    raw = next(
        r for r in log if r["event"] == "closed_label" and r["close_s"] == at(410)
    )
    assert raw["label"] == side and raw["entry_side"] == 0
    br = next(
        r for r in log if r["event"] == "structure_break" and r["close_s"] == at(418)
    )
    level = 92.281 if side == 1 else 107.719
    assert (
        br["kind"] == "choch"
        and br["bias"] == side
        and br["entry_side"] == 0
        and br["broken_level"] == level
    )
    assert rates[420]["low" if side == 1 else "high"] == level
    assert not any(
        r["event"]
        in ("batch_preview", "entry_quote_check", "demo_result", "live_hull_cross")
        for r in log
    )
    assert not mt.positions and not mt.orders and not mt.requests


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize(
    "close,expect", [(118.499, True), (118.5, True), (118.501, False)]
)
def test_exact_midpoint_included_on_actual_cross(side, close, expect):
    r = sequence(side, closes=PATH[:18] + [close], end_s=T)[-1]
    assert r.label == r.side == side and bool(r.entry_side) is expect
    assert r.structure.midpoint == (118.5 if side == 1 else 121.5)
    assert r.entry_audit()["range_midpoint"] == r.structure.midpoint


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("when", ["first_plan", "pre_send", "final_send", "next_leg"])
def test_one_tick_outside_midpoint_cannot_send_or_top_up(hull_desk, side, when):
    mt, desk, _ = hull_desk("medium")
    r = sig(1999 if side == 1 else 2001, side)
    # Exact executable midpoint on the first quote; just one tick beyond it thereafter.
    edge = mt.q.ask if side == 1 else mt.q.bid
    r = replace(
        r,
        structure=replace(
            r.structure,
            extreme=replace(
                r.structure.extreme, price=round(2 * edge - r.structure.origin.price, 3)
            ),
        ),
    )
    assert r.entry_side == side and r.structure.midpoint == edge
    first = mt.q
    drift = NS(
        time_msc=first.time_msc + 1,
        bid=round(first.bid + side * 0.001, 3),
        ask=round(first.ask + side * 0.001, 3),
    )
    calls = [0]

    def quote(_):
        calls[0] += 1
        return (
            drift
            if calls[0]
            >= {"first_plan": 1, "pre_send": 2, "final_send": 3, "next_leg": 4}[when]
            else first
        )

    mt.symbol_info_tick = quote
    expected = (
        ("protected_partial_no_topup:" if when == "next_leg" else "")
        + "quote_outside_"
        + r.structure.band(side).name
    )
    assert (
        desk.batch("XAUUSDm", r) == expected
        and len(mt.requests) == int(when == "next_leg")
        and not desk.waiting
    )
    assert desk.batch("XAUUSDm", r) == "already_consumed"


def test_unknown_clock_fails_closed_and_private_mapping_is_not_rewritten():
    cfg = load_yaml(ROOT / "config.yaml")["hull"]
    cfg["entry_timing"] = RETIRED_LIVE_TIMING
    assert (
        HullConfig.model_validate(cfg).entry_timing == CLOSE_ONLY
        and cfg["entry_timing"] == RETIRED_LIVE_TIMING
    )
    with pytest.raises(ValidationError):
        HullConfig.model_validate(cfg | {"entry_timing": "whatever"})
