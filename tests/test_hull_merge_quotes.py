"""Remote one-tick drift + local live-continuation safety. No broker or real sends."""

from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_mt import T, sig

from bot.hull.signal import LiveTick

NAME = "XAUUSDm"


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("live", [False, True])
def test_final_price_can_move_one_tick_but_exact_sent_request_was_checked(
    hull_desk, side, live
):
    mt, desk, _ = hull_desk("medium")
    r = sig(1999.95 if side == 1 else 2000.55, side=side)
    first = mt.q
    if live:
        r = replace(r, close_s=T + 60, trigger=LiveTick.read(first))
    moved = NS(time_msc=first.time_msc + 20, bid=2000.001, ask=2000.101)
    q = iter([first, first])  # move between pre_send and final_send of leg 1
    mt.symbol_info_tick = lambda _: next(q, moved)
    verified = []

    def still_crossed(name, reading, tick):
        verified.append(tick.time_msc)
        return name == NAME and reading == r and tick == moved

    desk.live_check = still_crossed
    checks = []
    precheck = mt.order_check

    def check(req):
        checks.append(dict(req))
        return precheck(req)

    mt.order_check = check
    assert desk.batch(NAME, r) == "demo_batch_sent"
    assert len(mt.requests) == len(mt.positions) == 4
    assert len(checks) == 5 and checks[1:] == mt.requests
    assert (
        checks[0]["comment"] == checks[1]["comment"]
        and checks[0]["price"] != checks[1]["price"]
    )
    assert all(
        req["price"] == (moved.ask if side == 1 else moved.bid) for req in mt.requests
    )
    assert bool(verified) == live and not desk.state.get("manual_hold")
    assert desk.batch(NAME, r) == "already_consumed"


@pytest.mark.parametrize(
    "case,outcome",
    [
        ("spread", "spread_cap"),
        ("stop", "swing_breached_or_too_close"),
        ("risk", "portfolio_risk_limit"),
        ("orders", "pending_orders_present"),
        ("funding", "insufficient_free_margin"),
        ("broker", "broker_precheck_rejected"),
        ("vanished", "live_cross_no_longer_verified"),
        ("two_ticks", "final_quote_or_safety_changed"),
        ("moved_again", "final_quote_or_safety_changed"),
    ],
)
def test_one_tick_is_not_a_safety_bypass_or_a_reprice_loop(hull_desk, case, outcome):
    mt, desk, _ = hull_desk("medium")
    first = mt.q
    r = replace(
        sig(1999.999 if case == "stop" else 1999.95),
        close_s=T + 60,
        trigger=LiveTick.read(first),
    )
    if case == "spread":
        key, p = desk.syms[NAME]
        desk.syms[NAME] = key, p.model_copy(update={"max_spread": 0.1})
    if case == "risk":
        desk.lim = desk.lim.model_copy(update={"max_open_risk_usd": 6.01})
    desk.live_check = lambda *_: case != "vanished"
    moved = NS(
        time_msc=first.time_msc + 20,
        bid=1999.999 if case == "stop" else first.bid,
        ask=2000.102 if case == "two_ticks" else 2000.101,
    )
    samples = 0

    def tick(_):
        nonlocal samples
        samples += 1
        if samples < 3:
            return first
        if samples == 3 and case == "orders":
            mt.orders.append(91)
        if samples == 3 and case == "funding":
            mt.a.margin_free = 0.01
        if samples >= 6 and case == "moved_again":
            return NS(time_msc=moved.time_msc + 20, bid=first.bid, ask=first.ask)
        return moved

    mt.symbol_info_tick = tick
    checks = []

    def check(req):
        checks.append(dict(req))
        return NS(
            retcode=1 if case == "broker" and len(checks) == 2 else 0,
            margin_free=8000.0,
        )

    mt.order_check = check
    assert desk.batch(NAME, r) == outcome
    assert (
        len(checks) <= 2
        and not mt.requests
        and not desk.waiting
        and not desk.state.get("manual_hold")
    )
    assert desk.batch(NAME, r) == "already_consumed"


def test_one_nonexecuting_tick_at_pre_send_with_same_plan_is_allowed(hull_desk):
    mt, desk, _ = hull_desk("medium")
    first = mt.q
    q = iter([first])
    moved = NS(time_msc=first.time_msc + 20, bid=2000.001, ask=first.ask)
    mt.symbol_info_tick = lambda _: next(q, moved)
    assert desk.batch(NAME, sig()) == "demo_batch_sent"
    assert len(mt.requests) == 4 and all(r["price"] == first.ask for r in mt.requests)
