from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_mt import MT, ROOT, T, sig

from bot.config.loader import load_yaml
from bot.execution.demo_runner import DemoSafetyError
from bot.hull.config import FIXED, HullConfig, selection
from bot.hull.desk import identity_check, resolve
from bot.hull.entry_clock import WAITING
from bot.hull.orders import Block, Grid, target
from bot.hull.terminal import Stream, run


@pytest.mark.parametrize(
    ("rate", "sl", "legs"),
    [
        ("fast", 1999.95, 7),
        ("fast", 1999.85, 7),
        ("medium", 1999.80, 4),
        ("medium", 1999.65, 4),
        ("slow", 1999.1, 10),
    ],
)
def test_exact_batch_range_per_ticket_tp_dynamic_sl_no_five_dollar_clip(
    hull_desk, rate, sl, legs
):
    mt, d, logs = hull_desk(rate)
    s = sig(sl)
    assert d.batch("XAUUSDm", s) == "demo_batch_sent"
    assert len(mt.positions) == legs
    for p in mt.positions:
        assert p.sl == sl and p.volume == 0.1 and p.magic == d.h.magic
        assert (p.tp - p.price_open) * 10 >= 11 - 1e-8 and (
            p.tp - 0.001 - p.price_open
        ) * 10 < 11 - 1e-8
    if rate == "slow":
        assert (mt.q.ask - sl) * 10 > 5
    assert len(d.ladders.book) == legs and not d.state.get("manual_hold")
    assert d.batch("XAUUSDm", s) == "already_consumed"
    assert any(x[0] == "batch_preview" for x in logs)


def test_nothing_sent_if_batch_minimum_does_not_fit(hull_desk):
    mt, d, log = hull_desk("fast")
    d.lim = d.lim.model_copy(update={"max_open_risk_usd": 0.1})
    assert d.batch("XAUUSDm", sig(1999.1)) == "portfolio_risk_limit"
    veto = next(data for name, data in log if name == "risk_veto")
    assert veto["min_legs"] == 6 and veto["min_batch_risk"] == pytest.approx(60)
    assert not mt.requests


def test_shadow_and_unknown_ack_never_resubmit(hull_desk):
    mt, d, _ = hull_desk(armed=False)
    assert d.batch("XAUUSDm", sig()) == "shadow_plan_only" and not mt.requests
    mt, d, _ = hull_desk("fast")
    mt.bad = True
    assert "manual_hold" in d.batch("XAUUSDm", sig())
    assert len(mt.requests) == 1
    _, d2, _ = hull_desk("fast", state=json.loads(d.path.read_text()), mt=mt)
    assert d2.batch("XAUUSDm", sig()) == "already_consumed"
    assert (
        d2.batch("XAUUSDm", replace(sig(), close_s=T + 60))
        == "manual_reconciliation_required"
    )
    assert len(mt.requests) == 1


def test_fixed_mode_retargets_to_actual_open_and_cash_exit_is_preserved(hull_desk):
    mt, d, _ = hull_desk()
    d.h = d.h.model_copy(update={"exit_policy": FIXED})
    mt.retprice = 2000.12
    assert d.batch("XAUUSDm", sig()) == "demo_batch_sent"
    assert (
        len(mt.positions) == 10
        and len(mt.requests) == 20
        and mt.requests[0]["action"] == 1
        and mt.requests[1]["action"] == 6
        and mt.positions[0].tp == pytest.approx(2001.22)
    )
    mt.positions[0].profit = 10.99
    d.supervise()
    assert len(mt.positions) == 10
    mt.positions[0].profit = 11.0
    d.supervise()
    assert len(mt.positions) == 9


def test_opposite_hull_label_closes_held_side_before_reversal(hull_desk):
    mt, d, _ = hull_desk("slow")
    mt.q.time_msc = (T - 1) * 1000 + 100
    buy = replace(sig(side=1), close_s=T - 1)
    assert d.batch("XAUUSDm", buy) == "demo_batch_sent"
    assert len(mt.positions) == 10

    mt.q.time_msc = T * 1000 + 100
    sell = replace(sig(sl=2000.05, side=-1), close_s=T)
    assert d.close_opposite("XAUUSDm", sell) == 10
    assert not mt.positions


def test_protection_entry_age_foreign_positions_and_kill_ownership(hull_desk):
    mt, d, _ = hull_desk()
    mt.q.time_msc -= 10000
    assert d.batch("XAUUSDm", sig()) == WAITING
    assert not mt.requests and ("XAUUSDm", 1) in d.waiting
    mt, d, _ = hull_desk()
    mt.positions = [NS(ticket=1, symbol="XAUUSDm", magic=99, sl=1, tp=2)]
    assert d.batch("XAUUSDm", sig()) == "foreign_or_unprotected_exposure"
    d.kill.touch()
    d.supervise()
    assert not mt.requests
    mt, d, _ = hull_desk()
    assert d.batch("XAUUSDm", sig()) == "kill"
    d.kill.unlink()
    mt, d, _ = hull_desk()
    mt.i.trade_stops_level = 200
    assert (
        d.batch("XAUUSDm", sig()) == "swing_breached_or_too_close" and not mt.requests
    )
    mt, d, _ = hull_desk()
    mt.orders.append(1)
    assert d.batch("XAUUSDm", sig()) == "pending_orders_present"


def test_conflicting_broker_aliases_bad_lots_or_real_account_fail_closed(hull_desk):
    cfg = load_yaml(ROOT / "config.yaml")
    h = HullConfig.model_validate(cfg["hull"])
    p = selection(h)
    mt = MT()
    mt.catalog.append(NS(name="XAUUSD"))
    with pytest.raises(DemoSafetyError, match="exact matches"):
        resolve(mt, p)
    p["XAUUSD"] = p["XAUUSD"].model_copy(
        update={"broker_symbol": "XAUUSDm", "lots": 0.001}
    )
    with pytest.raises(Block, match="illegal_lot"):
        resolve(mt, p)
    mt.a.trade_mode = 2
    with pytest.raises(DemoSafetyError):
        identity_check(mt, "demo", True)
    with pytest.raises(DemoSafetyError, match="demo"):
        run(cfg, h, p, armed=True)


def test_select_profiles_and_fx_currency_conversion():
    h = HullConfig.model_validate(load_yaml(ROOT / "config.yaml")["hull"])
    s = selection(h, profiles=["XAUUSD:0.09:10:fast", "USDJPY:0.02:8:slow"])
    assert s["USDJPY"].target_usd == 8 and s["XAUUSD"].rate == "fast"
    with pytest.raises(ValueError):
        selection(h, ["EURUSD"], ["XAUUSD:0.09:10:fast"])
    with pytest.raises(ValueError):
        selection(h, ["NOT_A_SYMBOL"])
    g = Grid(0.001, 0.001, 3, 0.001, 0.01, 100, 0.01)
    cash = lambda side, lots, entry, px: side * (px - entry) * 100000 * lots / px
    for side in (-1, 1):
        t, val = target(g, side, 0.02, 152.032, 8.0, cash)
        assert val >= 8 and cash(side, 0.02, 152.032, t - side * g.tick) < 8
    with pytest.raises(Block):
        target(g, 1, 0.02, 152.032, 8.0, lambda *a: None)


def test_stream_ignores_forming_bar_and_never_replays_startup_labels():
    from bot.hull.config import Indicator

    fix = json.loads((ROOT / "research/hull_labels/reference.json").read_text())
    j = next(i for i, r in enumerate(fix["sets"][0]["rows"]) if i > 80 and r[2])
    tf = 2
    dt = tf * 60
    data = [dict(time=T + i * dt, **r) for i, r in enumerate(fix["bars"])]
    end = [j]
    calls = []

    def rates(n, period, shift, count):
        calls.append((period, shift))
        return data[max(0, end[0] - count) : end[0]]

    mt = NS(TIMEFRAME_M2=2, copy_rates_from_pos=rates)
    ref = fix["sets"][0]
    s = Stream(2, Indicator(hull_len=ref["len"], source=ref["source"]), 0.01)
    assert s.refresh(mt, "S", T + j * dt + 0.2) == (None, "warmup_only")
    s.baseline_due = False  # verified first live snapshot already sampled
    end[0] = j + 2
    r, _ = s.refresh(mt, "S", T + (j + 1) * dt + 0.2)
    assert r.label == fix["sets"][0]["rows"][j][2] and r.close_s == T + (j + 1) * dt
    assert s.refresh(mt, "S", T + (j + 1) * dt + 0.3)[0] is None and all(
        pair == (2, 1) for pair in calls
    )


def test_uncertain_second_leg_does_not_retry_or_open_remaining_tickets(hull_desk):
    mt, d, _ = hull_desk("fast")
    send = mt.order_send

    def second_fails(r):
        if mt.requests:
            mt.bad = True
        return send(r)

    mt.order_send = second_fails
    assert "manual_hold" in d.batch("XAUUSDm", sig())
    assert len(mt.requests) == 2 and len(mt.positions) == 1 and mt.positions[0].sl > 0
    assert d.batch("XAUUSDm", sig()) == "already_consumed"


def test_final_quote_changed_between_precheck_and_send(hull_desk):
    mt, d, _ = hull_desk("medium")
    base = mt.order_check

    def revised(req):
        mt.q = NS(time_msc=T * 1000 + 105, bid=2000.03, ask=2000.13)
        return base(req)

    mt.order_check = revised
    assert d.batch("XAUUSDm", sig()) == "quote_or_plan_changed" and not mt.requests


@pytest.mark.parametrize(
    "explicit", [False, True], ids=["defaults", "older-local-config"]
)
def test_preflight_resolves_only_requested_symbols_and_never_sends(
    tmp_path, monkeypatch, capsys, explicit
):
    import sys

    mt = MT()
    cfg = load_yaml(ROOT / "config.yaml")
    if explicit:
        cfg["hull"]["limits"].update(
            max_positions=60,
            max_total_lots=54,
            max_open_risk_usd=1000,
            min_free_margin_pct=60,
            drawdown_usd=75,
            max_orders_minute=40,
        )
    h = HullConfig.model_validate(cfg["hull"])
    h = h.model_copy(update={"eligible_timeframes": ("M15", "M30")})
    mt.copy_rates_from_pos = lambda *args: []
    mt.initialize = lambda: True
    mt.shutdown = lambda: None
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt)
    monkeypatch.chdir(tmp_path)
    answer = run(cfg, h, selection(h), preflight=True)
    assert (
        answer["timeframes"] == ["M15", "M30"]
        and answer["signal_policy"] == "fc_dynamic_source_internal_v1"
        and answer["entry_timing"] == "live_hull_cross"
        and answer["entry_quote_policy"] == "current_live_tick_or_confirmed_close_v1"
        and answer["entry_sessions"]["version"] == "symbol-entry-sessions/v1"
        and answer["specs"]["XAUUSDm"]["entry_session"]["windows"]
        == ["london_ny_overlap"]
        and answer["specs"]["XAUUSDm"]["bid_chart_verified"]
        and answer["batch_entry_policy"] == "first_eligible_while_lane_flat_v2"
        and list(answer["specs"]) == ["XAUUSDm"]
        and not mt.requests
    )
    assert answer["mode"] == "READ ONLY" and not list(tmp_path.glob("reports/*"))
    assert answer["limits"] == h.limits.model_dump()
    assert answer["retired_limits"] == ["drawdown_usd", "max_orders_minute"]
    assert set(answer["retired_limits"]).isdisjoint(answer["limits"])
    ignored = [
        msg
        for msg in answer["review_before_demo"]
        if "Retired Hull limits ignored" in msg
    ]
    assert ignored == (
        ["Retired Hull limits ignored in this config: drawdown_usd, max_orders_minute"]
        if explicit
        else []
    )
    notices = [
        msg
        for msg in answer["review_before_demo"]
        if "Optional Hull entry limits" in msg
    ]
    if explicit:
        assert len(notices) == 1 and all(
            f"{k}={v}" in notices[0]
            for k, v in (
                ("max_positions", 60),
                ("max_total_lots", 54),
                ("max_open_risk_usd", 1000),
                ("min_free_margin_pct", 60),
            )
        )
    else:
        assert not notices


@pytest.mark.parametrize("armed", [False, True])
def test_legacy_saved_halt_and_full_send_window_do_not_block_or_reset_state(
    hull_desk, armed
):
    old = {"guard": {"dd_halt": True, "peak": 10500.0}, "sends": [T] * 40}
    state = {**old, "consumed": {"OLD": T - 60}}
    mt, d, _ = hull_desk(armed=armed, state=state)
    assert d.batch("XAUUSDm", sig()) == (
        "demo_batch_sent" if armed else "shadow_plan_only"
    )
    assert len(mt.requests) == (10 if armed else 0)
    if armed:
        saved = json.loads(d.path.read_text())
        assert saved["guard"] == old["guard"] and saved["sends"] == old["sends"]
        assert saved["consumed"] == {"OLD": T - 60}
        assert saved["lane_consumed"] == {"XAUUSDm": {"1": T}}
        _, restarted, _ = hull_desk(armed=True, mt=mt, state=saved)
        assert (
            restarted.batch("XAUUSDm", sig()) == "already_consumed"
            and len(mt.requests) == 10
        )
