"""Post-close one-tick drift safety. No broker or real sends."""

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

    checks = []
    precheck = mt.order_check

    def check(req):
        checks.append(dict(req))
        return precheck(req)

    mt.order_check = check
    if live:
        desk.verify_signal = lambda *args: True  # even if the projection verifies
        assert (
            desk.batch(NAME, r) == "await_cross_candle_close"
            and not checks
            and not mt.requests
        )
        return
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
    assert not desk.state.get("manual_hold")
    assert desk.batch(NAME, r) == "already_consumed"


@pytest.mark.parametrize(
    "case,outcome",
    [
        ("spread", "spread_cap"),
        ("stop", "initial_stop_breached_or_too_close"),
        ("risk", "portfolio_risk_limit"),
        ("orders", "pending_orders_present"),
        ("funding", "insufficient_free_margin"),
        ("broker", "broker_precheck_rejected"),
        ("projection", "next_bar_quote_or_context_unverified"),
        ("two_ticks", "final_quote_or_safety_changed"),
        ("moved_again", "final_quote_or_safety_changed"),
    ],
)
def test_one_tick_is_not_a_safety_bypass_or_a_reprice_loop(hull_desk, case, outcome):
    mt, desk, _ = hull_desk("medium")
    first = mt.q
    r = sig(1999.999 if case == "stop" else 1999.95)
    if case == "projection":
        desk.verify_signal = lambda *_: (
            False
        )  # a closed candidate still needs an executable current context
    if case == "spread":
        key, p = desk.syms[NAME]
        desk.syms[NAME] = key, p.model_copy(update={"max_spread": 0.1})
    if case == "risk":
        desk.lim = desk.lim.model_copy(update={"max_open_risk_usd": 6.01})
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


@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize("moved_again", [False, True])
def test_pre_send_spread_change_uses_the_one_unsent_reprice(
    hull_desk, side, moved_again
):
    mt, desk, log = hull_desk("medium")
    r = sig(1999.95 if side == 1 else 2000.55, side=side)
    first = mt.q
    # One tick narrower on the non-entry side: same entry, tighter SELL stop and risk; new goal/TP.
    moved = NS(
        time_msc=first.time_msc + 20,
        bid=2000.001 if side == 1 else first.bid,
        ask=2000.099 if side == -1 else first.ask,
    )
    # First plan / pre_send / unsent reprice / pre_send / final_send.
    quotes = iter([first, moved, moved, moved])
    final = (
        NS(time_msc=moved.time_msc + 20, bid=first.bid, ask=first.ask)
        if moved_again
        else moved
    )
    mt.symbol_info_tick = lambda _: next(quotes, final)
    checks = []

    def check(req):
        checks.append(dict(req))
        return NS(retcode=0, margin_free=8900.0)

    mt.order_check = check
    status = desk.batch(NAME, r)
    assert (
        checks[0]["price"] == checks[1]["price"] and checks[0]["tp"] != checks[1]["tp"]
    )
    assert [req["sl"] for req in checks[:2]] == (
        [2000.650, 2000.649] if side == -1 else [1999.95, 1999.95]
    )
    assert not desk.state.get("manual_hold") and not desk.waiting
    if moved_again:
        assert (
            status == "final_quote_or_safety_changed"
            and len(checks) == 2
            and not mt.requests
        )
    else:
        assert (
            status == "demo_batch_sent"
            and len(mt.positions) == 4
            and checks[1:] == mt.requests
        )
        for event, log_entry in log:
            if event == "demo_result":
                assert log_entry["entry_spread_usd"] == pytest.approx(0.99)
                assert log_entry["initial_goal_usd"] == pytest.approx(10.99)
                assert log_entry["request"]["tp"] == checks[1]["tp"]
    assert desk.batch(NAME, r) == "already_consumed"


def test_changed_cash_conversion_without_quote_move_is_not_a_reprice_license(hull_desk):
    mt, desk, _ = hull_desk("medium")
    cash = mt.order_calc_profit
    checks = []

    def price(kind, name, lots, entry, mark):
        value = cash(kind, name, lots, entry, mark)
        return (
            value * 1.1 if checks and (entry, mark) == (mt.q.ask, mt.q.bid) else value
        )

    def check(req):
        checks.append(dict(req))
        return NS(retcode=0, margin_free=8900.0)

    mt.order_calc_profit, mt.order_check = price, check
    assert desk.batch(NAME, sig()) == "quote_or_plan_changed"
    assert len(checks) == 1 and not mt.requests and not desk.state.get("manual_hold")


def test_multi_leg_batch_survives_fast_tick_jitter_and_sends_all_legs_in_burst(
    hull_desk,
):
    mt, desk, _ = hull_desk("slow")
    desk.h = desk.h.model_copy(
        update={"retest_market_fallback": True, "retest_quote_drift_ticks": 5}
    )
    first = mt.q
    r = sig(1999.95, side=1)
    ticks = 0
    service_calls = 0
    watch_calls = 0

    def tick(_):
        nonlocal ticks
        ticks += 1
        # Simulate fast tick jitter on leg 2 (moves twice within tolerance before stabilizing).
        if ticks in (5, 6):
            return NS(
                time_msc=first.time_msc + ticks * 5,
                bid=first.bid + 0.002,
                ask=first.ask + 0.002,
            )
        if ticks in (7, 8):
            return NS(
                time_msc=first.time_msc + ticks * 5,
                bid=first.bid + 0.004,
                ask=first.ask + 0.004,
            )
        return NS(
            time_msc=first.time_msc + ticks * 5,
            bid=first.bid + 0.004,
            ask=first.ask + 0.004,
        )

    def count_service():
        nonlocal service_calls
        service_calls += 1

    def count_watch(_):
        nonlocal watch_calls
        watch_calls += 1

    mt.symbol_info_tick = tick
    desk.service_if_due = count_service
    desk.watch_other_symbols = count_watch
    assert desk.batch(NAME, r) == "demo_batch_sent"
    assert len(mt.requests) == len(mt.positions) == 10
    assert service_calls == 1 and watch_calls == 1
