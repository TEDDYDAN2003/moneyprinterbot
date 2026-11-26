"""Exercise the real pricing and batch paths on a fake USD hedging terminal only."""

import json
import math
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_mt import ROOT, T, sig

from bot.config.loader import load_yaml
from bot.hull.config import FIXED, LADDER, TARGET_POLICY
from bot.hull.desk import Desk
from bot.hull.ladder import cash_levels
from bot.hull.orders import Block, Grid, make_plan, spread_goal, target
from bot.hull.signal import LiveTick
from bot.hull.structure import Structure
from bot.hull.terminal import target_preview


def us30(hull_desk, side, *, cap=15, base=10, rate="slow", policy=LADDER, armed=True):
    mt, desk, log = hull_desk(rate, armed=armed)
    name = "US30m"
    mt.i.name = name
    mt.i.trade_contract_size = 1
    mt.i.trade_tick_size = mt.i.point = 0.01
    mt.i.digits = 2
    mt.q = NS(bid=49_000.0, ask=49_013.0, time_msc=T * 1000 + 100)

    def profit(kind, sym, lots, entry, mark):
        assert sym == name
        return (1 if kind == mt.ORDER_TYPE_BUY else -1) * lots * (mark - entry)

    mt.order_calc_profit = profit
    prof = desk.h.symbols["US30"].model_copy(
        update={"rate": rate, "target_usd": base, "max_spread": cap}
    )
    desk.syms = {name: ("US30", prof)}
    desk.h = desk.h.model_copy(update={"exit_policy": policy})
    r = replace(
        sig(sl=48_980 if side == 1 else 49_033, side=side),
        close=mt.q.bid,
        structure=Structure(closed_s=T, bias=side),
    )
    return mt, desk, log, r


def events(log, event):
    return [r for e, r in log if e == event]


@pytest.mark.parametrize("side,tp", [(1, 49036.0), (-1, 48977.0)])
@pytest.mark.parametrize("policy", [LADDER, FIXED])
def test_thirteen_dollar_cushion_per_filled_ticket_not_thirteen_for_batch(
    hull_desk, side, tp, policy
):
    mt, d, log, r = us30(hull_desk, side, policy=policy)
    assert d.batch("US30m", r) == "demo_batch_sent"
    assert len(mt.requests) == len(mt.positions) == 10
    assert all(
        p.tp == tp and p.sl == r.raw_sl + (13 if side == -1 else 0) and p.volume == 1
        for p in mt.positions
    )
    assert all(abs(p.tp - p.price_open) == 23 for p in mt.positions)
    assert all(
        x["initial_goal_usd"] == 23
        and x["entry_spread_usd"] == 13
        and x["configured_target_usd"] == 10
        and x["target_policy"] == TARGET_POLICY
        and x["send_ask"] - x["send_bid"] == 13
        for x in events(log, "demo_result")
    )
    assert events(log, "batch_preview")[0]["goal_per_leg"] == 23
    if policy == LADDER:
        assert all(
            x["base_usd"] == 23 and x["entry_spread_usd"] == 13
            for x in d.ladders.book.values()
        )
        x = cash_levels(23, 1)
        assert [
            x[k] for k in ("trigger_usd", "lock_usd", "target_usd")
        ] == pytest.approx([16.10, 13.80, 27.60])
    else:
        assert list(d.state["target_by_ticket"].values()) == [23] * 10


@pytest.mark.parametrize("side", [1, -1])
def test_last_unsent_quote_each_sibling_and_actual_fill_have_distinct_roles(
    hull_desk, side
):
    mt, d, log, r = us30(hull_desk, side, rate="medium", base=8)
    # Keep the executable entry fixed but vary just the spread across the four legs.
    entry = mt.q.ask if side == 1 else mt.q.bid
    spreads = (13.0, 9.0, 4.0, 11.0)
    mt.retprice = entry + side * 0.25
    real_send = mt.order_send
    n = 0

    def send(req):
        nonlocal n
        result = real_send(req)
        if req["action"] == mt.TRADE_ACTION_DEAL and "position" not in req:
            n += 1
            # Already-filled ticket may now see a different quote during alignment.
            s = spreads[min(n, 3)]
            mt.q = NS(
                bid=entry - s if side == 1 else entry,
                ask=entry if side == 1 else entry + s,
                time_msc=T * 1000 + 100 + 10 * n,
            )
        return result

    mt.order_send = send
    assert d.batch("US30m", r) == "demo_batch_sent"
    saved = json.loads(d.path.read_text())["ladder_by_ticket"]
    receipts = events(log, "demo_result")
    assert (
        n == 4 and len(mt.requests) == 8
    )  # one protected entry + one price alignment per leg, no entry retry
    for s, p, ev in zip(spreads, mt.positions, receipts, strict=True):
        goal = 8 + s
        record = saved[str(p.ticket)]
        assert (
            ev["entry_spread_usd"],
            ev["initial_goal_usd"],
            ev["send_ask"] - ev["send_bid"],
        ) == (s, goal, s)
        assert ev["request"]["tp"] == entry + side * goal
        assert (p.tp, p.sl, p.price_open) == (
            mt.retprice + side * goal,
            r.raw_sl + (s if side == -1 else 0),
            mt.retprice,
        )
        assert record["base_usd"] == goal and record["entry_spread_usd"] == s


def test_broker_points_are_not_dollars_cap_still_blocks_even_a_high_goal(hull_desk):
    mt, d, log, r = us30(hull_desk, 1, cap=3)
    assert (
        d.batch("US30m", r) == "spread_cap"
        and not mt.requests
        and not events(log, "demo_result")
    )
    # Same instrument, 13 *platform points* of .01; now the spread is $0.13, not $13.
    mt, d, log, r = us30(hull_desk, -1, cap=3, armed=False)
    mt.q.ask = 49_000.13
    assert d.batch("US30m", r) == "shadow_plan_only" and not mt.requests
    x = events(log, "batch_preview")[0]
    assert (x["entry_spread_usd"], x["goal_per_leg"], x["tp"]) == pytest.approx(
        (0.13, 10.13, 48989.87)
    )


@pytest.mark.parametrize("failure", [None, math.nan, math.inf, True, "error", 0.5])
def test_unpriceable_spread_does_not_fall_back_to_base(hull_desk, failure):
    mt, d, _, r = us30(hull_desk, 1)
    original = mt.order_calc_profit
    mt.order_calc_profit = lambda k, n, l, e, x: (
        failure if (e, x) == (mt.q.ask, mt.q.bid) else original(k, n, l, e, x)
    )
    assert d.batch("US30m", r) == (
        "invalid_spread_valuation" if failure == 0.5 else "cash_conversion_unavailable"
    )
    assert not mt.requests and not d.ladders.book


def test_zero_and_subcent_broker_valuation():
    def no_call(*args):
        raise AssertionError("no spread conversion needed")

    assert spread_goal(1, 1, 100, 100, 7, no_call) == (0, 7)
    assert spread_goal(-1, 0.01, 1.0001, 1.00011, 5, lambda *_: -0.0) == (0, 5)


@pytest.mark.parametrize("side", [1, -1])
def test_nonlinear_account_cash_values_instead_of_contract_or_pip_guess(
    hull_desk, side
):
    mt, d, _, r = us30(hull_desk, side)
    bid, ask, lots = 152.033, 152.048, 0.07

    def cash(s, l, e, x):
        return s * l * 100000 * (x - e) / x

    p = d.syms["US30m"][1].model_copy(update={"lots": lots, "target_usd": 7})
    r = replace(
        r,
        close=bid,
        raw_sl=bid - 0.2 if side == 1 else ask + 0.2,
        structure=Structure(closed_s=T, bias=side),
    )
    g = Grid(0.005, 0.001, 3, 0, 0.01, 100, 0.01)
    plan = make_plan(r, p, g, bid, ask, cash)
    entry, mark = (ask, bid) if side == 1 else (bid, ask)
    cost = lots * 100000 * (ask - bid) / mark
    assert (plan.entry, plan.entry_spread_usd, plan.goal_usd) == pytest.approx(
        (entry, cost, 7 + cost)
    )
    assert (
        cash(side, lots, entry, plan.tp) >= 7 + cost - 1e-9
        and cash(side, lots, entry, plan.tp - side * g.tick) < 7 + cost
    )
    mt.order_calc_profit = lambda k, n, l, e, x: cash(1 if k == 0 else -1, l, e, x)
    previews = target_preview(mt, "US30m", p, LiveTick(T * 1000, bid, ask), g, d.h, T)
    test = previews["BUY" if side == 1 else "SELL"]
    assert test["entry_spread_usd"] == pytest.approx(cost)
    assert previews["BUY"]["entry_spread_usd"] != previews["SELL"]["entry_spread_usd"]
    assert test["ladder_preview_per_ticket"][0]["trigger_usd"] == pytest.approx(
        0.7 * (7 + cost)
    )
    late = target_preview(mt, "US30m", p, LiveTick(T * 1000, bid, ask), g, d.h, T + 5)
    assert all(v == {"status": "quote_unavailable_or_stale"} for v in late.values())


def test_legacy_open_ladder_uses_durable_original_base_and_levels_after_restart(
    hull_desk,
):
    mt, d, _, r = us30(hull_desk, 1, rate="medium")
    assert d.batch("US30m", r) == "demo_batch_sent"
    p = mt.positions[0]
    mt.positions = [p]
    state = json.loads(d.path.read_text())
    key = str(p.ticket)
    rec = state["ladder_by_ticket"][key]
    state["ladder_by_ticket"] = {key: rec}
    # Old-format position actually protected by a $10 TP, with no spread policy fields.
    for k in ("configured_target_usd", "entry_spread_usd", "target_policy"):
        del rec[k]
    rec["base_usd"] = 10
    rec["tp"] = p.tp = p.price_open + 10
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["run"]["mode"] = "demo"
    d2 = Desk(mt, cfg, d.h, d.syms, d.ident, state, d.path, d.emit, True)
    mt.requests.clear()
    mt.q = NS(bid=p.price_open + 7, ask=p.price_open + 8, time_msc=T * 1000 + 250)
    d2.supervise()
    assert (
        len(mt.requests) == 1
        and mt.positions[0].sl == p.price_open + 6
        and p.tp == p.price_open + 12
    )
    assert (
        d2.ladders.book[key]["base_usd"] == 10
        and "entry_spread_usd" not in d2.ladders.book[key]
    )


@pytest.mark.parametrize("side", [1, -1])
def test_fast_path_exactly_prices_tp_and_neighbor_within_existing_checks(
    hull_desk, side
):
    mt, d, _, r = us30(hull_desk, side)
    p = d.syms["US30m"][1]
    g = Grid.from_info(mt.i)
    calls = []

    def cash(s, l, e, x):
        calls.append(x)
        return s * l * (x - e)

    plan = make_plan(r, p, g, mt.q.bid, mt.q.ask, cash)
    assert len(calls) == 4  # risk, spread loss, candidate TP, preceding tick
    assert calls[-2:] == pytest.approx([plan.tp, plan.tp - side * g.tick])
    assert plan.reward_usd == 23


@pytest.mark.parametrize("slope", [None, 0.01, 0.7, 1.0, 11.0, 1e9])
@pytest.mark.parametrize("side", [1, -1])
def test_hint_changes_call_count_not_cash_or_minimal_tradable_tick(slope, side):
    g = Grid(0.25, 0.01, 2, 0, 0.01, 100, 0.01)
    e = 150.14

    def cash(d, l, entry, px):
        return round(12 * d * l * (px - entry) / px, 2)

    for usd in (0.01, 0.19, 0.61, 1.73, 5.0):
        px, value = target(g, side, 1, e, usd, cash, cash_per_price=slope)
        assert (
            px > 0
            and side * (px - e) > 0
            and value == cash(side, 1, e, px)
            and value >= usd
            and cash(side, 1, e, px - side * g.tick) < usd
        )
    with pytest.raises(Block, match="unreachable_cash_target"):
        target(g, side, 1, e, 12, lambda *_: 0, cash_per_price=slope)


def test_fixed_exit_at_exact_broker_cent_after_restart(hull_desk):
    mt, d, _, r = us30(hull_desk, -1, policy=FIXED, rate="medium")
    mt.q.ask = mt.q.bid + 1.12
    cash = mt.order_calc_profit
    mt.order_calc_profit = lambda *args: round(
        cash(*args), 2
    )  # broker account-currency cents
    assert d.batch("US30m", r) == "demo_batch_sent"
    p = mt.positions[0]
    mt.positions = [p]
    mt.requests.clear()
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["run"]["mode"] = "demo"
    # New config is unrelated to this ticket's recorded fixed cash exit.
    syms = {"US30m": ("US30", d.syms["US30m"][1].model_copy(update={"target_usd": 99}))}
    restart = Desk(
        mt,
        cfg,
        d.h.model_copy(update={"exit_policy": LADDER}),
        syms,
        d.ident,
        json.loads(d.path.read_text()),
        d.path,
        d.emit,
        True,
    )
    p.profit = 11.11
    restart.supervise()
    assert not mt.requests
    p.profit = 11.12
    restart.supervise()
    assert not mt.positions and len(mt.requests) == 1


def test_preflight_does_not_display_a_guessed_goal_for_an_unpriced_side(hull_desk):
    mt, d, _, _ = us30(hull_desk, 1, armed=False)
    p = d.syms["US30m"][1]
    q = LiveTick.read(mt.q)
    g = Grid.from_info(mt.i)
    original = mt.order_calc_profit
    mt.order_calc_profit = lambda kind, *rest: (
        None if kind == mt.ORDER_TYPE_BUY else original(kind, *rest)
    )
    sides = target_preview(mt, "US30m", p, q, g, d.h, T + 0.3)
    assert (
        sides["BUY"] == {"status": "cash_conversion_unavailable"}
        and sides["SELL"]["initial_goal_usd"] == 23
    )
    assert sides["SELL"]["batch_target_range_usd"] == [230, 230]
    wide = target_preview(
        mt, "US30m", p.model_copy(update={"max_spread": 3}), q, g, d.h, T + 0.3
    )
    assert all(v == {"status": "spread_cap"} for v in wide.values()) and not mt.requests
