"""Regression for the earlier hard-BE rollout, plus verified new-ticket config/enrollment."""

import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_mt import ROOT, sig
from test_hull_ladder import NAME, filled, log_for, quote

from bot.config.loader import load_yaml
from bot.hull.config import FILL_BREAKEVEN, FIXED, PAIRS, PULLBACK_HALF, HullConfig
from bot.hull.ladder import EarlyStop, Protection, propose, still_valid
from bot.hull.orders import Grid


def prior_policy(d):
    """Reconstruct the saved record shape of a ticket enrolled by the previous release."""
    for r in d.ladders.book.values():
        assert r["breakeven_policy"] == PULLBACK_HALF and "pullback_profit" not in r
        r["breakeven_policy"] = FILL_BREAKEVEN
        r.pop("initial_sl")
    d.persist()


def legacy_filled(*args, **kwargs):
    mt, d, log, p = filled(*args, **kwargs)
    prior_policy(d)
    return mt, d, log, p


@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize("distance", ["trade_stops_level", "trade_freeze_level"])
def test_first_legal_executable_quote_not_chart_touch(hull_desk, side, distance):
    mt, d, log, p = legacy_filled(
        hull_desk, side, slip=2000.123 if side == 1 else 1999.987
    )
    r = d.ladders.book[str(p.ticket)]
    original = p.sl, p.tp
    setattr(mt.i, distance, 40)  # .040 in price, > one tick
    p.profit = 99999  # never the trigger
    for delta in (-0.03, 0, 0.039):
        quote(mt, p, delta * 100 * p.volume, spread=0.2)
        d.supervise()
        assert (p.sl, p.tp) == original and not mt.requests
    quote(mt, p, 0.040 * 100 * p.volume, spread=0.2)
    d.supervise()
    assert mt.requests == [
        {
            "action": mt.TRADE_ACTION_SLTP,
            "sl": p.price_open,
            "tp": original[1],
            "position": p.ticket,
            "symbol": NAME,
            "magic": d.h.magic,
        }
    ]
    assert (
        r["breakeven_policy"] == FILL_BREAKEVEN
        and r["breakeven_confirmed"]
        and r["rung"] == 0
    )
    assert (
        log_for(log, "profit_ladder_breakeven_confirmed")[0]["previous_sl"]
        == original[0]
    )
    assert not log_for(log, "profit_ladder_step")
    d.supervise()
    quote(mt, p, 0.06 * 100 * p.volume)
    d.supervise()
    assert len(mt.requests) == 1
    quote(mt, p, 0)
    d.supervise()  # the server stop, not the scanner, now owns the touch
    assert r["broker_exit_seen"]["barrier"] == "sl" and len(mt.requests) == 1


@pytest.mark.parametrize("side,expected", [(1, 100.025), (-1, 100.0)])
def test_off_grid_average_fill_rounds_in_favor_and_never_loosens(side, expected):
    grid = Grid(0.025, 0.001, 3, 0, 0.01, 100, 0.01)
    entry = 100.012
    cash = lambda s, l, o, c: s * l * 100 * (c - o)
    old = Protection(0, 99.25 if side == 1 else 100.75, 101.0 if side == 1 else 99.0)

    def at(mark, old_levels=old):
        return propose(
            grid,
            side,
            1,
            entry,
            1000,
            old_levels,
            mark,
            cash,
            early=EarlyStop(FILL_BREAKEVEN),
        )

    assert at(expected) is None and at(expected + side * 0.024) is None
    mark = expected + side * 0.025
    new = at(mark)
    assert new == Protection(0, expected, old.tp)
    assert new.tp == old.tp and still_valid(
        grid,
        side,
        1,
        entry,
        1000,
        old,
        new,
        mark,
        cash,
        early=EarlyStop(FILL_BREAKEVEN),
    )
    assert not still_valid(
        grid,
        side,
        1,
        entry,
        1000,
        old,
        new,
        expected,
        cash,
        early=EarlyStop(FILL_BREAKEVEN),
    )
    assert at(expected + side * 0.1, replace(old, sl=expected + side * 0.025)) is None


@pytest.mark.parametrize("side", [1, -1])
def test_direct_rung_or_legal_fill_only_if_normal_lock_is_too_close(hull_desk, side):
    for minimum_gap in (0, 201):
        mt, d, log, p = legacy_filled(hull_desk, side)
        r = d.ladders.book[str(p.ticket)]
        tp = p.tp
        mt.i.trade_freeze_level = minimum_gap
        quote(mt, p, 7.75)
        d.supervise()
        if minimum_gap:
            assert (
                r["rung"] == 0
                and p.sl == p.price_open
                and p.tp == tp
                and len(mt.requests) == 1
            )
            mt.i.trade_freeze_level = 0
            quote(mt, p, 7.80)
            d.supervise()
        assert (
            r["rung"] == 1
            and p.sl == pytest.approx(p.price_open + side * 0.66)
            and p.tp == pytest.approx(p.price_open + side * 1.32)
        )
        assert len(log_for(log, "profit_ladder_breakeven_confirmed")) == int(
            bool(minimum_gap)
        )
        assert len(log_for(log, "profit_ladder_step")) == 1


@pytest.mark.parametrize(
    "change",
    [
        "retreat",
        "tp_touch",
        "new_rung",
        "stale",
        "older_quote",
        "changed_grid",
        "edited_tp",
        "kill",
        "rejected",
    ],
)
def test_last_recheck_does_not_send_obsolete_fill_stop(hull_desk, change):
    mt, d, log, p = legacy_filled(hull_desk)
    quote(mt, p, 0.1)
    old = p.sl, p.tp

    def checked(_):
        if change == "retreat":
            quote(mt, p, 0)
        if change == "tp_touch":
            quote(mt, p, 11)
        if change == "new_rung":
            quote(mt, p, 7.8)
        if change == "stale":
            mt.q.time_msc -= 5000
        if change == "older_quote":
            quote(mt, p, 0.1, stamp=mt.q.time_msc - 1)
        if change == "changed_grid":
            mt.i.trade_freeze_level = 199
        if change == "edited_tp":
            p.tp -= 0.1
        if change == "kill":
            d.kill.touch()
        return NS(retcode=7 if change == "rejected" else 0)

    mt.order_check = checked
    d.supervise()
    r = d.ladders.book[str(p.ticket)]
    assert (
        not r.get("breakeven_confirmed")
        and not mt.requests
        and "pending" not in r
        and p.sl == old[0]
    )
    assert not log_for(log, "profit_ladder_breakeven_confirmed")
    if change == "new_rung":
        mt.order_check = lambda _: NS(retcode=0)
        d.supervise()
        assert r["rung"] == 1 and len(mt.requests) == 1 and p.tp > old[1]


@pytest.mark.parametrize("reverse", [False, True])
def test_ticket_policy_cache_and_restart_isolation(hull_desk, reverse):
    mt, d, _, p = legacy_filled(hull_desk, single=False)
    ps = mt.positions
    book = d.ladders.book
    del book[str(ps[0].ticket)]["breakeven_policy"]  # actual old record shape
    book[str(ps[1].ticket)]["breakeven_policy"] = "off"
    eligible = {ps[2].ticket, ps[3].ticket}
    d.persist()
    _, resumed, log = hull_desk(mt=mt, state=json.loads(d.path.read_text()))
    resumed.h = resumed.h.model_copy(update={"pre_ladder_breakeven": False})
    if reverse:
        ps.reverse()
    quote(mt, p, 1)
    resumed.supervise()
    assert {x["position"] for x in mt.requests} == eligible
    assert len(log_for(log, "profit_ladder_breakeven_confirmed")) == 2
    assert all((x.sl == x.price_open) == (x.ticket in eligible) for x in ps)
    mt.requests.clear()
    quote(mt, p, 8.2)
    resumed.supervise()
    assert {x["position"] for x in mt.requests} == {x.ticket for x in ps}
    assert all(v["rung"] == 1 for v in resumed.ladders.book.values())


def test_uncertain_ack_is_durable_and_never_blindly_retried(hull_desk):
    mt, d, log, p = legacy_filled(hull_desk)
    key = str(p.ticket)
    real = mt.order_send

    def lost(req):
        pending = json.loads(d.path.read_text())["ladder_by_ticket"][key]["pending"]
        assert pending["kind"] == FILL_BREAKEVEN and pending["rung"] == 0
        mt.requests.append(dict(req))  # no acknowledgement and no bracket read-back yet

    mt.order_send = lost
    quote(mt, p, 1)
    d.supervise()
    assert d.ladders.needs_review() and not log_for(
        log, "profit_ladder_breakeven_confirmed"
    )
    _, d2, log2 = hull_desk(mt=mt, state=json.loads(d.path.read_text()))
    d2.h = d2.h.model_copy(update={"pre_ladder_breakeven": False})
    quote(mt, p, 8.2)
    d2.supervise()
    assert d2.ladders.needs_review() and len(mt.requests) == 1
    req = mt.requests[0]
    p.sl = req["sl"]
    p.tp = req["tp"] + 0.1
    d2.supervise()
    assert d2.ladders.needs_review()  # both levels, not just SL
    p.tp = req["tp"]
    d2.supervise()
    r = d2.ladders.book[key]
    assert r["breakeven_confirmed"] and r["rung"] == 0 and not d2.ladders.needs_review()
    assert (
        len(mt.requests) == 1
        and len(log_for(log2, "profit_ladder_breakeven_confirmed")) == 1
    )
    mt.order_send = real
    d2.supervise()
    assert r["rung"] == 1 and len(mt.requests) == 2


@pytest.mark.parametrize(
    "case",
    [
        "observer",
        "foreign",
        "partial",
        "manual_hold",
        "stale",
        "already_sl",
        "already_tp",
    ],
)
def test_no_unsafe_or_unowned_amendment(hull_desk, case):
    mt, d, log, p = legacy_filled(hull_desk)
    original = p.sl, p.tp
    quote(mt, p, 1)
    if case == "observer":
        d.armed = False
    if case == "foreign":
        p.magic = -1
    if case == "partial":
        p.volume *= 0.5
    if case == "manual_hold":
        d.state["manual_hold"] = {"reason": "operator review"}
    if case == "stale":
        mt.q.time_msc -= 9000
    if case == "already_sl":
        quote(mt, p, -50)
    if case == "already_tp":
        quote(mt, p, 15)
    d.supervise()
    assert (
        (p.sl, p.tp) == original
        and not mt.requests
        and not log_for(log, "profit_ladder_breakeven_confirmed")
    )


def test_private_yaml_omission_defaults_on_but_fixed_tp_stays_fixed(hull_desk):
    raw = load_yaml(ROOT / "config.yaml")["hull"]
    assert raw.pop("pre_ladder_breakeven") is True
    assert HullConfig.model_validate(raw).new_ticket_breakeven_policy == PULLBACK_HALF
    assert (
        HullConfig.model_validate(
            raw | {"pre_ladder_breakeven": False}
        ).new_ticket_breakeven_policy
        == "off"
    )
    with pytest.raises(ValueError):
        HullConfig.model_validate(raw | {"pre_ladder_breakeven": "maybe"})
    (
        mt,
        d,
        _,
    ) = hull_desk("medium")
    d.h = d.h.model_copy(update={"exit_policy": FIXED})
    assert d.h.new_ticket_breakeven_policy == "off"
    assert d.batch(NAME, sig(sl=1999.6)) == "demo_batch_sent" and not d.ladders.book
    p = mt.positions[0]
    old = p.sl, p.tp
    mt.requests.clear()
    quote(mt, p, 1)
    d.supervise()
    assert not mt.requests and (p.sl, p.tp) == old


@pytest.mark.parametrize("side", [1, -1])
def test_retest_enrollment_after_proven_fill_only(tmp_path, monkeypatch, side):
    from test_hull_direct_retest import make

    mt, d, r, log, _ = make(tmp_path, monkeypatch, side)
    child = PAIRS[r.tf]
    opened = r.close_s - child * 60
    r = replace(
        r,
        trigger_tf=child,
        anchor_s=opened,
        window_start_s=opened,
        cross_bar=replace(r.cross_bar, t=opened),
    )
    assert d.batch(NAME, r) == "retest_batch_placed"
    assert len(mt.orders) == 10 and not mt.positions and not d.ladders.book
    d.supervise()
    assert all(x["action"] == mt.TRADE_ACTION_PENDING for x in mt.requests)
    mt.now += 2
    first = mt.orders[0]
    p = mt.fill(first, improve=0.04)
    assert p.price_open != first.price_open
    tp = round(
        first.tp - side * 0.04, mt.i.digits
    )  # existing TP alignment to better fill
    # First supervised executable quote is already in profit on this fill.
    mt.tick(p.price_open + 0.03 if side == 1 else p.price_open - 0.09, spread=0.06)
    d.supervise()
    amended = [q for q in mt.requests if q["action"] == mt.TRADE_ACTION_SLTP]
    assert [(q["position"], q["sl"], q["tp"]) for q in amended] == [
        (p.ticket, first.sl, tp)
    ]
    rec = d.ladders.book[str(p.ticket)]
    assert rec["initial_sl"] == first.sl and rec["pullback_profit"] and p.sl == first.sl
    mt.now += 0.1
    mt.tick(p.price_open - 0.01 if side == 1 else p.price_open - 0.05, spread=0.06)
    d.supervise()
    last = mt.requests[-1]
    assert last["action"] == mt.TRADE_ACTION_SLTP and last["position"] == p.ticket
    assert (
        p.sl == pytest.approx((first.sl + p.price_open) / 2, abs=0.0051)
        and p.sl != p.price_open
    )
    assert last["tp"] == p.tp == tp and len(mt.orders) == 9
    assert (
        rec["rung"] == 0
        and rec["pullback_half_confirmed"]
        and len(
            [e for e in log if e["event"] == "profit_ladder_pullback_half_confirmed"]
        )
        == 1
    )


def test_definite_no_change_vs_success_ack_without_readback(hull_desk, monkeypatch):
    mt, d, log, p = legacy_filled(hull_desk)
    key = str(p.ticket)
    old = (p.sl, p.tp)
    mono = [10.0]
    from bot.hull import ladder_desk

    monkeypatch.setattr(ladder_desk, "time", NS(time=d.now, monotonic=lambda: mono[0]))
    mt.TRADE_RETCODE_INVALID_STOPS = 10016
    code = [10016]

    def refuse_or_ack(req):
        mt.requests.append(dict(req))
        return NS(retcode=code[0])  # position stays at old levels

    mt.order_send = refuse_or_ack
    quote(mt, p, 0.4)
    d.supervise()
    r = d.ladders.book[key]
    assert (
        len(mt.requests) == 1
        and (p.sl, p.tp) == old
        and not r.get("pending")
        and not r.get("breakeven_confirmed")
    )
    quote(mt, p, 0.5)
    d.supervise()
    assert len(mt.requests) == 1  # definite refusal backs off
    code[0] = mt.TRADE_RETCODE_DONE
    mono[0] += 1.1
    quote(mt, p, 0.6)
    d.supervise()
    assert (
        len(mt.requests) == 2
        and r["pending"]["kind"] == FILL_BREAKEVEN
        and not r.get("breakeven_confirmed")
    )
    quote(mt, p, 8)
    d.supervise()
    assert len(mt.requests) == 2 and not log_for(
        log, "profit_ladder_breakeven_confirmed"
    )


@pytest.mark.parametrize("side", [1, -1])
def test_one_quote_four_distinct_slipped_fills_only_eligible_siblings_amend(
    hull_desk, side
):
    mt, d, log = hull_desk("medium")
    mt.i.trade_tick_size = 0.025
    mt.i.trade_freeze_level = 50  # .050
    requested = mt.q.ask if side == 1 else mt.q.bid
    prices = [
        round(requested + side * k, 3) for k in (0.012, 0.038, 0.062, 0.113)
    ]  # actual, off grid
    floors = [round(requested + side * k, 3) for k in (0.025, 0.050, 0.075, 0.125)]
    next_fill = iter(prices)
    send = mt.order_send

    def fill_each(req):
        if req["action"] == mt.TRADE_ACTION_DEAL and "position" not in req:
            mt.retprice = next(next_fill)
        return send(req)

    mt.order_send = fill_each
    assert (
        d.batch(NAME, sig(sl=1999.6 if side == 1 else 2000.6, side=side))
        == "demo_batch_sent"
    )
    prior_policy(d)
    ps = mt.positions
    assert [p.price_open for p in ps] == prices
    original_sl = ps[0].sl
    tps = [p.tp for p in ps]
    mt.requests.clear()
    for distance, eligible in ((0.1, 2), (0.2, 4)):
        mark = round(requested + side * distance, 3)
        mt.q = NS(
            time_msc=mt.q.time_msc + 1,
            bid=mark if side == 1 else mark - 0.1,
            ask=mark + 0.1 if side == 1 else mark,
        )
        d.supervise()
        assert [(q["position"], q["sl"], q["tp"]) for q in mt.requests] == [
            (p.ticket, sl, tp) for p, sl, tp in zip(ps[:eligible], floors, tps)
        ]
        assert [p.sl for p in ps] == floors[:eligible] + [original_sl] * (
            4 - eligible
        ) and [p.tp for p in ps] == tps
        assert (
            not log_for(log, "profit_ladder_step")
            and len(log_for(log, "profit_ladder_breakeven_confirmed")) == eligible
        )
