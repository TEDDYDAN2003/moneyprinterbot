"""Discretionary Hull caps vs required broker funding. All terminal calls are simulated."""

import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_mt import ROOT, T, sig
from pydantic import ValidationError

from bot.config.loader import load_yaml
from bot.execution.demo_runner import DemoSafetyError
from bot.hull.config import HullConfig, Limits
from bot.hull.orders import Exposure, Plan, room

NAME = "XAUUSDm"
OFF = {
    "max_positions": None,
    "max_total_lots": None,
    "max_open_risk_usd": None,
    "min_free_margin_pct": 0,
}


def events(log, name):
    return [row for event, row in log if event == name]


def test_shipped_and_omitted_settings_match_and_do_not_change_old_explicit_configs():
    cfg = load_yaml(ROOT / "config.yaml")
    h = HullConfig.model_validate(cfg["hull"])
    for limits in (h.limits, Limits()):
        assert {k: getattr(limits, k) for k in OFF} == OFF
        assert {"drawdown_usd", "max_orders_minute"}.isdisjoint(limits.model_dump())
    assert cfg["risk"]["min_free_margin_pct"] == 60  # not used by the Hull executor
    old = {
        "max_positions": 60,
        "max_total_lots": 54.0,
        "max_open_risk_usd": 1000.0,
        "min_free_margin_pct": 60.0,
    }
    # A private full config can retain the retired keys, without hiding typos or
    # stripping the four independent opt-in portfolio/funding overrides.
    legacy = {**old, "drawdown_usd": 75, "max_orders_minute": 40}
    assert Limits.model_validate(legacy).model_dump() == Limits(**old).model_dump()
    assert Limits.model_validate(legacy).model_dump(include=set(old)) == old
    for bad in (
        {"max_order_minute": 40},  # only the two exact legacy keys are accepted
        {"max_positions": 0},
        {"max_total_lots": -1},
        {"max_open_risk_usd": float("inf")},
        {"min_free_margin_pct": -1},
        {"min_free_margin_pct": 101},
    ):
        with pytest.raises(ValidationError):
            Limits(**bad)


@pytest.mark.parametrize(
    "over,expected",
    [
        ({}, ""),
        ({"max_positions": 60}, "portfolio_position_limit"),
        ({"max_total_lots": 54.0}, "portfolio_lot_limit"),
        ({"max_open_risk_usd": 1000.0}, "portfolio_risk_limit"),
    ],
)
def test_each_cap_is_independent_and_still_opt_in(over, expected):
    # The replay and desk both call room(). A funded signal can exceed all three old ceilings.
    plan = Plan(1, 0.1, 2000.10, 1979.613, 2001.20, 204.87, 11.0, 10.0, 1.0)
    assert room(Limits(**over), Exposure(60, 54.0, 0.0), plan, 10) == expected


def test_mock_gold_2048_70_batch_risk_and_57_percent_free_keeps_all_tickets_protected(
    hull_desk,
):
    mt, d, log = hull_desk("slow")
    mt.a.margin_free = 6500.0  # equity $10,000
    checks = []
    send = mt.order_send

    def check(req):
        checks.append(req)
        return NS(retcode=0, margin_free=mt.a.margin_free - 80.0)

    def fill(req):
        result = send(req)
        mt.a.margin_free -= 80.0
        return result

    mt.order_check, mt.order_send = check, fill
    # Synthetic example at the incident's reported dollar risk, NOT the broker's historical quotes.
    result = d.batch(NAME, replace(sig(sl=1979.613), tf=5))
    assert (
        result == "demo_batch_sent"
        and len(mt.positions) == len(checks) == len(mt.requests) == 10
    )
    (preview,) = events(log, "batch_preview")
    assert (preview["risk_usd"], preview["risk_batch"]) == pytest.approx(
        (204.87, 2048.70)
    )
    assert mt.a.margin_free / mt.a.equity == pytest.approx(0.57) and not events(
        log, "margin_veto"
    )
    assert all(
        p.volume == 0.1 and p.sl == 1979.613 and p.tp > p.price_open
        for p in mt.positions
    )
    assert not d.state.get("manual_hold") and d.batch(NAME, sig()) == "already_consumed"


@pytest.mark.parametrize(
    "reserve,free,per_leg,outcome",
    [
        (0, 800.0, 80.0, "shadow_plan_only"),  # exactly funded
        (60, 6800.0, 80.0, "shadow_plan_only"),  # exactly at an explicit reserve
        (60, 6799.0, 80.0, "margin_floor"),  # funded but fails private reserve
        (
            0,
            799.0,
            80.0,
            "insufficient_free_margin",
        ),  # one leg fits, full slow batch doesn't
        (0, 9000.0, None, "no_margin_estimate"),
        (0, 9000.0, "bad", "no_margin_estimate"),
        (0, 9000.0, -1.0, "no_margin_estimate"),
        (0, None, 80.0, "no_margin_estimate"),
        (0, float("nan"), 80.0, "no_margin_estimate"),
        (0, 9000.0, float("inf"), "no_margin_estimate"),
    ],
)
def test_whole_batch_funding_estimate_and_numeric_audit(
    hull_desk, reserve, free, per_leg, outcome
):
    mt, d, log = hull_desk(armed=False)
    d.lim = d.lim.model_copy(update={"min_free_margin_pct": reserve})
    mt.a.margin_free = free
    mt.order_calc_margin = lambda *_: per_leg
    assert d.batch(NAME, sig()) == outcome and not mt.requests
    json.dumps(log, allow_nan=False)
    vetoes = events(log, "margin_veto")
    if outcome == "shadow_plan_only":
        assert not vetoes
    else:
        (v,) = vetoes
        assert (v["stage"], v["legs_checked"], v["reason"]) == (
            "first_plan",
            10,
            outcome,
        )
        assert (v["equity_usd"], v["required_free_usd"]) == (10000.0, 100.0 * reserve)
        if outcome != "no_margin_estimate":
            assert v["projected_free_usd"] == free - 10 * per_leg


@pytest.mark.parametrize(
    "reserve,retcode,free_after,why",
    [
        (0, 10019, None, "broker_precheck_rejected"),
        (0, 0, -1.0, "insufficient_free_margin"),
        (0, 0, None, "no_margin_estimate"),
        (0, 0, float("nan"), "no_margin_estimate"),
        (60, 0, 5900.0, "margin_floor"),
    ],
)
def test_broker_order_check_cannot_be_bypassed(
    hull_desk, reserve, retcode, free_after, why
):
    mt, d, log = hull_desk()
    d.lim = d.lim.model_copy(update={"min_free_margin_pct": reserve})
    mt.order_check = lambda req: NS(retcode=retcode, margin_free=free_after)
    assert (
        d.batch(NAME, sig()) == why
        and not mt.requests
        and len(events(log, "batch_preview")) == 1
    )
    if retcode:
        assert events(log, "broker_precheck_rejected")[0]["retcode"] == 10019
    else:
        (v,) = events(log, "margin_veto")
        assert (v["stage"], v["legs_checked"]) == ("order_check", 1)
    json.dumps(log, allow_nan=False)


def test_funding_rechecked_after_first_fill_without_topup(hull_desk):
    mt, d, log = hull_desk()
    original = mt.order_send

    def changed(req):
        res = original(req)
        mt.a.margin_free = 100.0
        return res

    mt.order_send = changed
    assert d.batch(NAME, sig()) == "protected_partial_no_topup:insufficient_free_margin"
    assert (
        d.batch(NAME, sig()) == "already_consumed"
        and len(mt.positions) == len(mt.requests) == 1
    )
    (p,) = mt.positions
    assert p.sl > 0 and p.tp > 0
    (v,) = events(log, "margin_veto")
    assert (v["stage"], v["legs_checked"], v["projected_free_usd"]) == (
        "next_leg",
        9,
        -620.0,
    )


def test_explicit_risk_cap_still_applies_to_actual_fill(hull_desk):
    mt, d, _ = hull_desk()
    d.lim = d.lim.model_copy(update={"max_open_risk_usd": 16.0})
    mt.retprice = 2004.0  # pre-send batch risk ~$15; unexpected fill now risks >$16 on this leg alone
    assert d.batch(NAME, sig()) == "fill_risk_exceeded_manual_hold"
    assert (
        len(mt.requests) == 2 and mt.requests[1]["position"] == 1 and not mt.positions
    )


def test_can_add_protected_gold_with_60_owned_tickets_and_54_lots_elsewhere(hull_desk):
    mt, d, _ = hull_desk()
    other = "EURUSDm"
    d.syms[other] = ("EURUSD", d.h.symbols["EURUSD"])
    mt.positions = [
        NS(
            ticket=100 + i,
            symbol=other,
            magic=d.h.magic,
            type=mt.POSITION_TYPE_BUY,
            sl=1.09,
            tp=1.11,
            volume=0.9,
            price_open=1.10,
            profit=0.0,
        )
        for i in range(60)
    ]
    assert d.batch(NAME, sig()) == "demo_batch_sent"
    assert len(mt.positions) == 70
    assert sum(p.volume for p in mt.positions) == pytest.approx(55.0)
    assert len(mt.requests) == 10 and all(r["symbol"] == NAME for r in mt.requests)


def test_disabled_risk_cap_does_not_skip_post_fill_exposure_reconciliation(hull_desk):
    mt, d, _ = hull_desk()
    send = mt.order_send

    def concurrent_pending(req):
        res = send(req)
        mt.orders.append(NS(ticket=9000))
        return res

    mt.order_send = concurrent_pending
    assert d.batch(NAME, sig()) == "manual_hold:pending_orders_present"
    assert len(mt.requests) == 1 and d.state["manual_hold"] and not d.ladders.book


def test_lower_allowed_count_can_fit_without_spurious_margin_veto(hull_desk):
    mt, d, log = hull_desk("medium", armed=False)
    mt.a.margin_free = 240.0
    assert d.batch(NAME, sig()) == "shadow_plan_only" and not mt.requests
    assert events(log, "batch_preview")[0]["legs"] == 3 and not events(
        log, "margin_veto"
    )


def test_missing_broker_check_fails_closed(hull_desk):
    mt, d, log = hull_desk()
    mt.order_check = lambda req: None
    assert d.batch(NAME, sig()) == "broker_precheck_rejected" and not mt.requests
    assert events(log, "broker_precheck_rejected")[0]["retcode"] is None


def test_pre_send_rechecks_full_batch_after_positive_order_check(hull_desk):
    mt, d, log = hull_desk()

    def check(req):
        # This ticket fits, but updated free margin cannot fund the remaining batch.
        mt.a.margin_free = 799.0
        return NS(retcode=0, margin_free=719.0)

    mt.order_check = check
    assert d.batch(NAME, sig()) == "insufficient_free_margin"
    assert not mt.requests and "manual_hold" not in d.state
    (v,) = events(log, "margin_veto")
    assert (v["stage"], v["legs_checked"], v["projected_free_usd"]) == (
        "pre_send",
        10,
        -1.0,
    )


def test_eight_slow_batches_can_send_80_protected_entries_in_one_window(hull_desk):
    # Identical synthetic specs, independent symbols and signal reservations.
    # The fixture freezes wall time and supplies funded, broker-accepted quotes.
    mt, d, _ = hull_desk()
    _, profile = d.syms[NAME]
    names = [NAME, *(f"TEST{i}" for i in range(2, 9))]
    for name in names:
        d.syms[name] = (name, profile)
        assert d.batch(name, sig()) == "demo_batch_sent"
    assert len(mt.requests) == len(mt.positions) == len(d.ladders.book) == 80
    assert all(p.sl > 0 and p.tp > p.price_open for p in mt.positions)
    assert d.state["consumed"] == dict.fromkeys(names, T)
    assert "sends" not in d.state
    for name in names:
        assert d.batch(name, sig()) == "already_consumed"
    assert len(mt.requests) == 80


def test_equity_drop_between_funded_legs_does_not_interrupt_the_batch(hull_desk):
    mt, d, _ = hull_desk()
    send = mt.order_send

    def fill(req):
        answer = send(req)
        if len(mt.requests) == 1:
            mt.a.equity -= 100.0  # funded throughout, $100 below the first check
        return answer

    mt.order_send = fill
    assert d.batch(NAME, sig()) == "demo_batch_sent"
    assert mt.a.equity == 9900.0 and len(mt.requests) == 10


@pytest.mark.parametrize("equity", [0.0, -1.0, float("nan"), float("inf"), None])
def test_invalid_equity_is_not_treated_as_a_permitted_drawdown(hull_desk, equity):
    mt, d, _ = hull_desk()
    mt.a.equity = equity
    with pytest.raises(DemoSafetyError, match="Invalid account equity"):
        d.batch(NAME, sig())
    assert not mt.requests
