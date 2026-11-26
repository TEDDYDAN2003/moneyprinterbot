"""New-ticket pullback policy. All broker quotes, orders, fills and restarts are simulated."""

import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_mt import sig
from test_hull_ladder import NAME, filled, log_for, quote

from bot.hull.config import FILL_BREAKEVEN, PULLBACK_HALF
from bot.hull.ladder import EarlyStop, Protection, propose, still_valid
from bot.hull.orders import Grid

SEEN = "profit_ladder_pullback_profit_seen"
HALF = "profit_ladder_pullback_half_confirmed"


def sweep(mt, d, p, dist, **options):
    """Price distance on the profitable side of this ticket's fill; negative = loss."""
    mark = quote(mt, p, dist * 100 * p.volume, **options)
    d.supervise()
    return mark


@pytest.mark.parametrize(
    "side,fill,half", [(1, 2000.123, 1999.862), (-1, 1999.987, 2000.343)]
)
@pytest.mark.parametrize("return_distance", [0, -0.03], ids=["touch", "through_entry"])
def test_observed_profit_then_return_only_never_a_hard_be_or_rung(
    hull_desk, side, fill, half, return_distance
):
    mt, d, log, p = filled(hull_desk, side, slip=fill)
    r = d.ladders.book[str(p.ticket)]
    initial = (p.sl, p.tp)
    assert r["initial_sl"] == p.sl and r["breakeven_policy"] == PULLBACK_HALF
    p.profit = 999  # P/L display and the wrong side of the spread must not arm this
    for dist in (-0.04, 0, -0.015):
        sweep(mt, d, p, dist, spread=0.25)
        assert (
            (p.sl, p.tp) == initial and not mt.requests and "pullback_profit" not in r
        )
    p.profit = -10
    first = sweep(mt, d, p, 0.003)
    stamp = mt.q.time_msc
    for dist in (0.2, 0.1, 0.02):
        sweep(mt, d, p, dist)
        assert not mt.requests and (p.sl, p.tp) == initial
    assert (
        r["pullback_profit"] == {"time_msc": stamp, "mark": first}
        and len(log_for(log, SEEN)) == 1
    )
    sweep(mt, d, p, return_distance)
    assert mt.requests == [
        {
            "action": mt.TRADE_ACTION_SLTP,
            "position": p.ticket,
            "symbol": NAME,
            "magic": d.h.magic,
            "sl": half,
            "tp": initial[1],
        }
    ]
    assert (
        (p.sl, p.tp) == (half, initial[1])
        and r["rung"] == 0
        and r["pullback_half_confirmed"]
        and not r.get("breakeven_confirmed")
    )
    [event] = log_for(log, HALF)
    assert (
        event["entry"],
        event["previous_sl"],
        event["initial_sl"],
        event["profit_tick_msc"],
        event["rung"],
    ) == (fill, initial[0], initial[0], stamp, 0)
    assert not log_for(log, "profit_ladder_step") and not log_for(
        log, "profit_ladder_breakeven_confirmed"
    )
    for dist in (0.30, 0, -0.04):
        sweep(mt, d, p, dist)
    assert (
        len(mt.requests) == 1 and p.sl == half
    )  # midpoint is not moved again on a second pullback
    sweep(mt, d, p, 0.78)
    assert (
        r["rung"] == 1
        and len(mt.requests) == 2
        and p.sl == pytest.approx(fill + side * 0.66)
    )
    assert p.tp == pytest.approx(fill + side * 1.32) and r["initial_sl"] == initial[0]


@pytest.mark.parametrize(
    "side,original,expect",
    [(1, 90, 95), (-1, 110, 105), (1, 90.01, 95.025), (-1, 110.04, 105.0)],
)
def test_midpoint_geometry_is_an_initial_price_distance_not_current_stop_or_cash(
    side, original, expect
):
    g = Grid(0.025, 0.001, 3, 0, 0.01, 100, 0.01)
    entry = 100.0
    old = Protection(0, entry - side * 8, entry + side * 4)
    # Frozen original, not the current (already slightly tightened) stop or TP.
    early = EarlyStop(PULLBACK_HALF, original)
    cash = lambda s, l, a, b: s * (b - a) * l
    args = (g, side, 1, entry, 1000)
    pair = propose(*args, old, entry, cash, early=early)
    assert pair == Protection(0, expect, old.tp) and still_valid(
        *args, old, pair, entry, cash, early=early
    )
    assert not still_valid(
        *args, old, pair, entry + side * 0.1, cash, early=early
    )  # rebound
    for current_sl in (expect, expect + side * 0.025):
        assert (
            propose(*args, replace(old, sl=current_sl), entry, cash, early=early)
            is None
        )
    assert propose(*args, old, entry, cash) is None  # no historical proof supplied
    assert (
        propose(
            *args, old, entry, cash, early=EarlyStop(PULLBACK_HALF, entry + side * 0.1)
        )
        is None
    )


@pytest.mark.parametrize("side", [1, -1])
def test_observation_is_durable_but_not_a_license_for_same_or_older_ticks(
    hull_desk, side
):
    mt, d, log, p = filled(hull_desk, side)
    sweep(mt, d, p, 0.05)
    stamp = mt.q.time_msc
    saved = json.loads(d.path.read_text())
    (
        _,
        restarted,
        _,
    ) = hull_desk(mt=mt, state=saved)
    restarted.h = restarted.h.model_copy(
        update={"pre_ladder_breakeven": False}
    )  # future tickets only
    for when in (stamp - 1, stamp, stamp):
        sweep(mt, restarted, p, 0, stamp=when)
        assert not mt.requests
    sweep(mt, restarted, p, -0.01, stamp=stamp + 1)
    assert len(mt.requests) == 1
    assert (
        restarted.ladders.book[str(p.ticket)]["pullback_half_confirmed"]
        and len(log_for(log, SEEN)) == 1
    )


@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize(
    "distance",
    [
        "trade_stops_level",
        "trade_freeze_level",
        "through_midpoint",
        "at_midpoint",
        "tp_too_close",
    ],
)
def test_illegal_or_gapped_midpoint_does_not_move_sl_or_close_market(
    hull_desk, side, distance
):
    mt, d, log, p = filled(hull_desk, side)
    original = p.sl, p.tp
    rec = d.ladders.book[str(p.ticket)]
    sweep(mt, d, p, 0.10)
    half_distance = abs(p.price_open - p.sl) / 2
    if distance in ("trade_stops_level", "trade_freeze_level"):
        setattr(mt.i, distance, round(1000 * half_distance) + 1)
        sweep(mt, d, p, 0)
    elif distance == "tp_too_close":
        # Same original TP on the broker and in durable state. Only its distance fails.
        rec["tp"] = p.tp = p.price_open + side * 0.02
        original = (p.sl, p.tp)
        mt.i.trade_freeze_level = 50
        sweep(mt, d, p, 0)
    else:
        sweep(
            mt, d, p, -half_distance - (0.03 if distance == "through_midpoint" else 0)
        )
    assert not mt.requests and (p.sl, p.tp) == original and not log_for(log, HALF)
    mt.i.trade_freeze_level = mt.i.trade_stops_level = 0
    sweep(mt, d, p, 0.01)
    assert not mt.requests  # do not chase on the profitable side
    sweep(mt, d, p, -0.02)
    assert (
        len(mt.requests) == 1
        and mt.requests[0]["action"] == mt.TRADE_ACTION_SLTP
        and p.tp == original[1]
    )


@pytest.mark.parametrize("level", ["sl", "tp"])
def test_actual_hard_barrier_wins_even_after_witness(hull_desk, level):
    mt, d, _, p = filled(hull_desk)
    old = p.sl, p.tp
    sweep(mt, d, p, 0.1)
    sweep(mt, d, p, (p.sl if level == "sl" else p.tp) - p.price_open)
    sweep(mt, d, p, 0)
    assert (
        not mt.requests
        and (p.sl, p.tp) == old
        and d.ladders.book[str(p.ticket)]["broker_exit_seen"]["barrier"] == level
    )


@pytest.mark.parametrize("side", [1, -1])
def test_direct_normal_rung_never_waits_and_cannot_be_undone(hull_desk, side):
    mt, d, log, p = filled(hull_desk, side)
    sweep(mt, d, p, 0.78)
    r = d.ladders.book[str(p.ticket)]
    assert len(mt.requests) == 1 and r["rung"] == 1 and not log_for(log, HALF)
    strong = (p.sl, p.tp)
    sweep(mt, d, p, 0)
    assert (p.sl, p.tp) == strong and len(mt.requests) == 1


@pytest.mark.parametrize(
    "change",
    [
        "bounce",
        "below_mid",
        "sl_touch",
        "tp_touch",
        "fresh_rung",
        "kill",
        "edited_sl",
        "edited_tp",
        "grid",
        "rejected",
        "stale",
        "old",
    ],
)
def test_pullback_is_rechecked_after_broker_precheck(hull_desk, change):
    mt, d, log, p = filled(hull_desk)
    key = str(p.ticket)
    sweep(mt, d, p, 0.03)
    old = p.sl, p.tp

    def during(_):
        if change == "bounce":
            quote(mt, p, 0.3)
        if change == "below_mid":
            quote(mt, p, -3)
        if change == "sl_touch":
            quote(mt, p, -6)
        if change == "tp_touch":
            quote(mt, p, 12)
        if change == "fresh_rung":
            quote(mt, p, 7.8)
        if change == "kill":
            d.kill.touch()
        if change == "edited_sl":
            p.sl += 0.025
        if change == "edited_tp":
            p.tp -= 0.025
        if change == "grid":
            mt.i.trade_freeze_level = 600
        if change == "stale":
            mt.q.time_msc -= 8000
        if change == "old":
            mt.q.time_msc -= 1
        return NS(retcode=7 if change == "rejected" else 0)

    mt.order_check = during
    sweep(mt, d, p, 0)
    r = d.ladders.book[key]
    assert (
        not mt.requests
        and not r.get("pending")
        and not r.get("pullback_half_confirmed")
        and not log_for(log, HALF)
    )
    if change == "fresh_rung":
        mt.order_check = lambda _: NS(retcode=0)
        d.supervise()
        assert r["rung"] == 1 and len(mt.requests) == 1 and p.sl > p.price_open
    if change not in ("fresh_rung", "edited_sl", "edited_tp"):
        assert (p.sl, p.tp) == old


@pytest.mark.parametrize("reverse", [True, False])
def test_identical_siblings_cannot_share_the_profit_witness(hull_desk, reverse):
    mt, d, log, p = filled(hull_desk, single=False)
    ps = list(mt.positions)
    quote(mt, p, 1)
    d.ladders.supervise(
        ps[0], {}, {}, {}
    )  # only ticket zero observed a profitable quote
    if reverse:
        mt.positions.reverse()
    sweep(mt, d, p, 0)
    assert [q["position"] for q in mt.requests] == [ps[0].ticket]
    assert len(log_for(log, HALF)) == 1 and all(
        "pullback_profit" not in d.ladders.book[str(x.ticket)] for x in ps[1:]
    )


def test_new_and_legacy_policies_coexist_after_restart_no_loosening(hull_desk):
    mt, d, _, p = filled(hull_desk, single=False)
    new, hard, untagged, off = mt.positions
    r = d.ladders.book
    r[str(hard.ticket)]["breakeven_policy"] = FILL_BREAKEVEN
    r[str(hard.ticket)].pop("initial_sl")
    del r[str(untagged.ticket)]["breakeven_policy"]
    r[str(off.ticket)]["breakeven_policy"] = "off"
    d.persist()
    _, d, log = hull_desk(mt=mt, state=json.loads(d.path.read_text()))
    old = off.sl
    sweep(mt, d, p, 0.03)
    assert hard.sl == hard.price_open and new.sl == untagged.sl == off.sl == old
    sweep(mt, d, p, 0)
    assert hard.sl == hard.price_open and new.sl == pytest.approx(
        (new.price_open + old) / 2
    )
    assert (
        untagged.sl == off.sl == old
        and len(log_for(log, HALF)) == 1
        and len(mt.requests) == 2
    )


@pytest.mark.parametrize("kind", ["unknown", "done", "exception"])
def test_half_stop_pending_readback_never_resends_on_restart(hull_desk, kind):
    mt, d, log, p = filled(hull_desk)
    key = str(p.ticket)
    tp = p.tp
    sweep(mt, d, p, 0.02)

    def lost(req):
        pending = json.loads(d.path.read_text())["ladder_by_ticket"][key]["pending"]
        assert pending["kind"] == PULLBACK_HALF and pending["initial_sl"] == p.sl
        assert (
            pending["profit_tick_msc"] < pending["tick_time_msc"]
            and pending["profit_mark"] > p.price_open
        )
        mt.requests.append(dict(req))
        if kind == "exception":
            raise OSError("simulated lost connection")
        return NS(retcode=mt.TRADE_RETCODE_DONE) if kind == "done" else None

    mt.order_send = lost
    if kind == "exception":
        with pytest.raises(OSError):
            sweep(mt, d, p, -0.01)
    else:
        sweep(mt, d, p, -0.01)
    assert d.ladders.needs_review() and not log_for(log, HALF)
    _, again, log2 = hull_desk(mt=mt, state=json.loads(d.path.read_text()))
    r = again.ladders.book[key]
    again.supervise()
    p.sl = mt.requests[0]["sl"]
    p.tp = tp + 0.1
    again.supervise()
    assert again.ladders.needs_review() and not log_for(log2, HALF)
    p.tp = tp
    again.supervise()
    sweep(mt, again, p, 0.01)
    assert (
        len(mt.requests) == 1
        and r["pullback_half_confirmed"]
        and not again.ladders.needs_review()
        and r["rung"] == 0
        and len(log_for(log2, HALF)) == 1
    )


def test_definite_refusal_retries_only_on_later_eligible_return(hull_desk, monkeypatch):
    from bot.hull import ladder_desk

    mt, d, log, p = filled(hull_desk)
    sweep(mt, d, p, 0.04)
    old = p.sl, p.tp
    clock = [0.0]
    monkeypatch.setattr(ladder_desk, "time", NS(time=d.now, monotonic=lambda: clock[0]))
    real = mt.order_send
    mt.TRADE_RETCODE_FROZEN = 10029

    def refuse(req):
        mt.requests.append(dict(req))
        return NS(retcode=10029)

    mt.order_send = refuse
    sweep(mt, d, p, 0)
    assert (
        len(mt.requests) == 1
        and not d.ladders.needs_review()
        and (p.sl, p.tp) == old
        and not log_for(log, HALF)
    )
    mt.order_send = real
    sweep(mt, d, p, -0.01)
    assert len(mt.requests) == 1
    clock[0] += 1.1
    sweep(mt, d, p, 0.02)
    assert len(mt.requests) == 1
    sweep(mt, d, p, -0.02)
    assert len(mt.requests) == 2 and len(log_for(log, HALF)) == 1


@pytest.mark.parametrize("side", [1, -1])
def test_one_return_four_fills_not_batch_average(hull_desk, side):
    mt, d, log = hull_desk("medium")
    mt.i.trade_tick_size = 0.025
    req_entry = mt.q.ask if side == 1 else mt.q.bid
    fills = [round(req_entry + side * x, 3) for x in (0.012, 0.038, 0.062, 0.113)]
    # Original broker stops: BUY 1999.6; SELL 2000.6 raw + 0.1 spread = 2000.7.
    halves = (
        [1999.875, 1999.875, 1999.900, 1999.925]
        if side == 1
        else [2000.325, 2000.325, 2000.300, 2000.275]
    )
    actual = iter(fills)
    send = mt.order_send

    def per_leg(q):
        if q["action"] == mt.TRADE_ACTION_DEAL and "position" not in q:
            mt.retprice = next(actual)
        return send(q)

    mt.order_send = per_leg
    assert (
        d.batch(NAME, sig(sl=1999.6 if side == 1 else 2000.6, side=side))
        == "demo_batch_sent"
    )
    ps = mt.positions
    assert [p.price_open for p in ps] == fills
    mt.requests.clear()
    tps = [p.tp for p in ps]

    def sample(delta):
        mark = round(req_entry + side * delta, 3)
        mt.q = NS(
            time_msc=mt.q.time_msc + 1,
            bid=mark if side == 1 else mark - 0.1,
            ask=mark if side == -1 else mark + 0.1,
        )
        d.supervise()

    sample(0.05)
    assert not mt.requests and len(log_for(log, SEEN)) == 2
    sample(0)
    assert [q["position"] for q in mt.requests] == [p.ticket for p in ps[:2]]
    sample(0.15)
    sample(-0.025)
    assert (
        len(mt.requests) == 4
        and [p.sl for p in ps] == halves
        and [p.tp for p in ps] == tps
    )


@pytest.mark.parametrize(
    "case",
    [
        "observer",
        "foreign",
        "manual_hold",
        "stale",
        "future",
        "pre_fill",
        "partial",
        "external_sl",
    ],
)
def test_ineligible_quote_or_ticket_never_establishes_profit_history(hull_desk, case):
    mt, d, log, p = filled(hull_desk)
    r = d.ladders.book[str(p.ticket)]
    quote(mt, p, 1)
    if case == "observer":
        d.armed = False
    if case == "foreign":
        p.magic = -8
    if case == "manual_hold":
        d.state["manual_hold"] = {"reason": "review"}
    if case == "stale":
        mt.q.time_msc -= 30_000
    if case == "future":
        mt.q.time_msc += 30_000
    if case == "pre_fill":
        mt.q.time_msc = r["opened_msc"] - 1
    if case == "partial":
        p.volume /= 2
    if case == "external_sl":
        p.sl += 0.05
    old = p.sl, p.tp
    d.supervise()
    assert (
        (p.sl, p.tp) == old
        and not mt.requests
        and not log_for(log, SEEN)
        and "pullback_profit" not in r
    )


@pytest.mark.parametrize("side", [1, -1])
def test_no_legal_half_tick_is_not_a_reason_to_widen_or_market_exit(side):
    g = Grid(0.25, 0.01, 2, 0, 0.01, 100, 0.01)
    entry = 100.0
    origin = entry - side * 0.25
    old = Protection(0, origin, entry + side * 2)
    ctx = EarlyStop(PULLBACK_HALF, origin)
    # Gross midpoint rounds to entry. There is no legal, tighter on-grid SL on a return.
    for mark in (entry, entry - side * 0.1, entry + side * 0.25):
        assert ctx.candidate(g, side, entry, old, mark) is None
