"""Entry-only stop translation: real desk + persistent state, synthetic terminals only."""

import json
from contextlib import closing
from dataclasses import asdict, replace
from types import SimpleNamespace as NS

import pytest
from hull_mt import ROOT, T, sig
from test_hull_direct_retest import NAME, make
from test_hull_spread_targets import events, us30

from bot.hull.config import (
    LIVE_SPREAD_V1,
    PAIRS,
    PRE_SPREAD,
    PULLBACK_HALF,
    STOP_SPREAD_POLICY,
)
from bot.hull.desk import Desk
from bot.hull.ladder import Protection, propose
from bot.hull.orders import Block, Grid, Plan, spread_adjusted_stop


def market(hull_desk, side):
    mt, d, log, r = us30(hull_desk, side, rate="medium", base=8, cap=3)
    mt.q = NS(
        bid=98.0 if side == 1 else 100.0,
        ask=100.0 if side == 1 else 102.0,
        time_msc=mt.q.time_msc,
    )
    raw = 90 if side == 1 else 110
    return (
        mt,
        d,
        log,
        replace(
            r, close=mt.q.bid, raw_sl=raw, anchor=raw + side * 0.01, stop_tick_size=0.01
        ),
    )


def opened(hull_desk, side=-1):
    mt, d, log, r = market(hull_desk, side)
    assert d.batch("US30m", r) == "demo_batch_sent"
    p = mt.positions[0]
    mt.positions[:] = [p]
    mt.requests.clear()
    return mt, d, log, p


def sample(mt, d, side, mark, width=2):
    mt.q = NS(
        bid=mark if side == 1 else mark - width,
        ask=mark + width if side == 1 else mark,
        time_msc=mt.q.time_msc + 1,
    )
    d.supervise()


def restart(d, state):
    return Desk(
        d.mt,
        {"run": {"mode": "demo"}},
        d.h,
        d.syms,
        d.ident,
        state,
        d.path,
        d.emit,
        True,
    )


@pytest.mark.parametrize(
    "side,raw,initial,half,lock,extended",
    [(1, 90, 90, 95, 106, 112), (-1, 110, 112, 106, 94, 88)],
)
def test_one_translation_then_real_half_then_six_dollar_lock(
    hull_desk, side, raw, initial, half, lock, extended
):
    mt, d, log, p = opened(hull_desk, side)
    rec = d.ladders.book[str(p.ticket)]
    tp = 100 + side * 10
    assert (p.price_open, p.sl, p.tp, rec["base_usd"]) == (100, initial, tp, 10)
    assert (
        rec["raw_chart_sl"],
        rec["initial_sl"],
        rec["initial_stop_spread_buffer"],
        rec["stop_spread_policy"],
    ) == (raw, initial, initial - raw, STOP_SPREAD_POLICY)
    assert events(log, "batch_preview")[0]["risk_usd"] == abs(initial - 100)
    for e in events(log, "demo_result"):
        assert (
            e["frozen_cross_sl"] == e["raw_chart_sl"] == raw
            and e["request"]["sl"] == initial
        )
    sample(mt, d, side, 100 - side)
    sample(mt, d, side, 100)
    assert not mt.requests and "pullback_profit" not in rec  # touched only from loss
    sample(mt, d, side, 100 + side * 2)
    assert not mt.requests and rec["pullback_profit"]["mark"] == 100 + side * 2
    sample(mt, d, side, 100, width=4)  # only the executable mark matters now
    assert (p.sl, p.tp, rec["rung"]) == (half, tp, 0) and len(mt.requests) == 1
    [e] = events(log, "profit_ladder_pullback_half_confirmed")
    assert (e["modeled_stop_pnl_usd"], e["stop_spread_buffer"]) == (
        -abs(initial - 100) / 2,
        0,
    )
    sample(mt, d, side, 100, width=1)
    sample(mt, d, side, 100 + side * 7, width=3)
    assert (p.sl, p.tp, rec["rung"]) == (lock, extended, 1) and len(mt.requests) == 2
    [e] = events(log, "profit_ladder_step")
    assert (
        e["trigger_pct"],
        e["lock_pct"],
        e["modeled_lock_pct"],
        e["modeled_stop_pnl_usd"],
        e["target_pct"],
    ) == (70, 60, 60, 6, 120)
    sample(mt, d, side, 100 + side * 6.5, width=4)
    assert (
        len(mt.requests) == 2 and rec["initial_sl"] == initial and rec["base_usd"] == 10
    )


@pytest.mark.parametrize(
    "tick,digits,bid,ask,raw,sell,buffer",
    [
        (0.00001, 5, 1.10000, 1.10007, 1.10041, 1.10048, 0.00007),
        (0.00005, 5, 1.10000, 1.10007, 1.10040, 1.10050, 0.00010),
        (0.025, 3, 100, 100.026, 110.025, 110.075, 0.05),
        (0.1, 1, 100, 100, 110, 110, 0),
    ],
)
def test_decimal_quote_components_and_outward_trade_grid(
    tick, digits, bid, ask, raw, sell, buffer
):
    g = Grid(tick, 10**-digits, digits, 0, 0.01, 100, 0.01)
    assert spread_adjusted_stop(g, -1, raw, bid, ask) == (sell, buffer)
    assert spread_adjusted_stop(g, 1, raw, bid, ask) == (raw, 0)
    with pytest.raises(Block, match="bad_quote"):
        spread_adjusted_stop(g, -1, raw, ask, bid - 1)


def test_cash_cap_counts_translated_broker_risk_not_raw_wick(hull_desk):
    mt, d, _, r = market(hull_desk, -1)
    d.lim = d.lim.model_copy(
        update={"max_open_risk_usd": 35}
    )  # min 3 × 12 > 35; 3 × raw 10 would pass
    assert d.batch("US30m", r) == "portfolio_risk_limit" and not mt.requests


def test_last_valuation_cannot_send_positive_but_underfloor_short_lock(hull_desk):
    mt, d, log, p = opened(hull_desk)
    rec = d.ladders.book[str(p.ticket)]
    original = (p.sl, p.tp)
    checked = []
    cash = mt.order_calc_profit

    def revalue(kind, name, lots, entry, px):
        return 5.99 if checked and px == 94 else cash(kind, name, lots, entry, px)

    def check(req):
        checked.append(dict(req))
        return NS(retcode=0, margin_free=8900.0)

    mt.order_check, mt.order_calc_profit = check, revalue
    sample(mt, d, -1, 93)
    assert [(q["sl"], q["tp"]) for q in checked] == [(94, 88)]
    assert (
        (p.sl, p.tp) == original
        and not mt.requests
        and not events(log, "profit_ladder_step")
        and not rec.get("pending")
    )
    mt.order_calc_profit = cash
    sample(mt, d, -1, 93)
    assert (p.sl, p.tp, rec["rung"]) == (94, 88, 1) and len(mt.requests) == 1


def historical(rec, p, generation):
    if generation != STOP_SPREAD_POLICY:
        for k in ("stop_spread_policy", "raw_chart_sl", "initial_stop_spread_buffer"):
            del rec[k]
    if generation == PRE_SPREAD:
        del rec["stop_spread_buffer"]
        rec["initial_sl"] = rec["sl"] = p.sl = 110


@pytest.mark.parametrize(
    "generation,half,rung",
    [(PRE_SPREAD, 105, 94), (LIVE_SPREAD_V1, 108, 96), (STOP_SPREAD_POLICY, 106, 94)],
)
def test_restart_uses_existing_generation_for_each_next_amendment(
    hull_desk, generation, half, rung
):
    mt, d, _, p = opened(hull_desk)
    state = json.loads(d.path.read_text())
    key = str(p.ticket)
    historical(state["ladder_by_ticket"][key], p, generation)
    d = restart(d, state)
    assert not mt.requests
    sample(mt, d, -1, 98)
    sample(mt, d, -1, 100)
    assert (p.sl, p.tp) == (half, 90) and len(mt.requests) == 1
    saved = json.loads(d.path.read_text())
    assert saved["ladder_by_ticket"][key]["stop_spread_policy"] == generation
    d = restart(d, saved)
    sample(mt, d, -1, 93)
    assert (p.sl, p.tp) == (rung, 88) and len(mt.requests) == 2


@pytest.mark.parametrize(
    "generation,issued",
    [(PRE_SPREAD, 105), (LIVE_SPREAD_V1, 108), (STOP_SPREAD_POLICY, 106)],
)
def test_pending_restart_reads_original_pair_not_repriced_midpoint(
    hull_desk, generation, issued
):
    mt, d, log, p = opened(hull_desk)
    state = json.loads(d.path.read_text())
    key = str(p.ticket)
    r = state["ladder_by_ticket"][key]
    historical(r, p, generation)
    stamp = mt.q.time_msc + 1
    r["pullback_profit"] = {"mark": 98, "time_msc": stamp}
    intent = {
        "rung": 0,
        "sl": issued,
        "tp": 90,
        "kind": PULLBACK_HALF,
        "tick_time_msc": stamp + 1,
        "mark": 100,
        "initial_sl": r["initial_sl"],
        "previous_sl": r["sl"],
        "profit_tick_msc": stamp,
        "profit_mark": 98,
    }
    if generation != PRE_SPREAD:
        intent["stop_spread_buffer"] = 2 if generation == LIVE_SPREAD_V1 else 0
    if generation == STOP_SPREAD_POLICY:
        intent["stop_spread_policy"] = generation
    r["pending"] = dict(intent)
    d = restart(d, state)
    p.sl = issued
    p.tp = 91  # partial read-back; no replay even on a later fresh quote
    sample(mt, d, -1, 100, width=4)
    assert (
        r["pending"] == intent
        and not mt.requests
        and not events(log, "profit_ladder_pullback_half_confirmed")
    )
    p.tp = 90
    sample(mt, d, -1, 100, width=3)
    assert (
        "pending" not in r
        and r["stop_spread_policy"] == generation
        and r["sl"] == issued
    )
    assert (
        not mt.requests
        and len(events(log, "profit_ladder_pullback_half_confirmed")) == 1
    )


@pytest.mark.parametrize("missing", [False, True])
def test_unknown_generation_holds_does_not_adopt_latest_rules(hull_desk, missing):
    mt, d, _, p = opened(hull_desk)
    r = d.ladders.book[str(p.ticket)]
    if missing:
        del r["stop_spread_policy"]
    else:
        r["stop_spread_policy"] = "unknown"
    sample(mt, d, -1, 93)
    assert r["hold"] == "unknown_ladder_stop_policy_review" and not mt.requests


@pytest.mark.parametrize(
    "has_buffer,expected", [(False, PRE_SPREAD), (True, LIVE_SPREAD_V1)]
)
def test_old_serialized_limit_plan_uses_shape_before_new_defaults(has_buffer, expected):
    data = asdict(Plan(-1, 0.1, 100, 110, 90, 10, 10, 10, 0))
    for k in ("stop_spread_policy", "raw_chart_sl", "stop_spread_buffer"):
        data.pop(k)
    if has_buffer:
        data["stop_spread_buffer"] = 0.0  # zero-width spread is still a v1 placement
    restored = Plan.from_saved(data)
    assert restored.stop_spread_policy == expected and restored.raw_chart_sl is None
    assert (restored.sl, restored.tp, restored.goal_usd) == (110, 90, 10)


def retest(tmp, mp, side=-1):
    mt, d, r, log, factory = make(tmp, mp, side)
    child = PAIRS[r.tf]
    start = r.close_s - child * 60
    r = replace(
        r,
        trigger_tf=child,
        anchor_s=start,
        window_start_s=start,
        cross_bar=replace(r.cross_bar, t=start),
    )
    return mt, d, r, log, factory


@pytest.mark.parametrize("leg", [1, 2])
@pytest.mark.parametrize("fallback", [False, True])
def test_last_retest_quote_one_tick_wider_never_sends_underbuffered_leg(
    tmp_path, monkeypatch, leg, fallback
):
    mt, d, r, logs, _ = retest(tmp_path, monkeypatch)
    d.h = d.h.model_copy(update={"retest_market_fallback": fallback})
    check = mt.order_check

    def widen(req):
        res = check(req)
        if len(mt.checks) == leg:
            mt.now += 0.01
            mt.tick(2000, spread=0.07)
        return res

    mt.order_check = widen
    assert d.batch(NAME, r) == "retest_stop_spread_buffer_insufficient"
    assert len(mt.checks) == leg and len(mt.requests) == leg - 1
    assert mt.checks[-1]["sl"] == 2002.18 and all(
        q["sl"] == 2002.18 for q in mt.requests
    )
    assert not mt.positions and not any(
        e["event"] == "retest_market_fallback" for e in logs
    )
    assert d.retest.active[NAME]["legs"][-1]["status"] == "not_sent"


@pytest.mark.parametrize(
    "side,narrow,initial", [(1, False, 1997.88), (-1, True, 2002.18)]
)
def test_pending_send_quote_and_actual_journal_match(
    tmp_path, monkeypatch, side, narrow, initial
):
    mt, d, r, logs, _ = retest(tmp_path, monkeypatch, side)
    check = mt.order_check

    def move(req):
        res = check(req)
        if len(mt.checks) == 1:
            mt.now += 0.01
            mt.tick(2000, spread=0.05 if narrow else 0.07)
        return res

    mt.order_check = move
    assert d.batch(NAME, r) == "retest_batch_placed" and len(mt.orders) == 10
    assert all(q["sl"] == initial for q in mt.requests) and mt.checks == mt.requests
    # A later spread is not a price instruction. The limit order stays as accepted.
    mt.now += 2
    mt.tick(2000, spread=0.08)
    d.supervise()
    assert len(mt.orders) == 10 and not d.ladders.book and len(mt.requests) == 10
    x = mt.orders[0]
    mt.fill(x, flat=True)
    for other in list(mt.orders):
        mt.terminate(other, 6)
    d.supervise()
    [e] = [e for e in logs if e["event"] == "retest_fill_receipt"]
    assert (
        e["raw_chart_sl"],
        e["request"]["sl"],
        e["stop_reference_ask"],
        e["send_ask"],
    ) == (r.raw_sl, initial, 2000.06, 2000.05 if narrow else 2000.07)
    monkeypatch.syspath_prepend(str(ROOT / "HullJournal"))
    monkeypatch.syspath_prepend(str(ROOT / "HullJournal/tests"))
    from fixtures import FakeMT, deal
    from ledger import MAGIC, db_open, identity, make_views, sync

    di = deal(
        e["deal"],
        x.position_id,
        T + 2,
        int(side == -1),
        0,
        x.volume_initial,
        x.price_open,
        comment="rewritten",
        ms=e["fill_ms"] % 1000,
    )
    di.order = x.ticket
    out = deal(
        e["deal"] + 20000,
        x.position_id,
        T + 10,
        int(side == 1),
        1,
        x.volume_initial,
        x.tp,
        profit=10,
        magic=0,
    )
    broker = FakeMT([di, out], T + 250)
    broker.login, broker.host = 1, "t"
    folder = tmp_path / "reports/hull_runtime" / identity(broker, MAGIC)["key"]
    folder.mkdir(parents=True, exist_ok=True)
    # In particular, a SELL limit is proved by the placement reference AND an
    # adequate send buffer; it does not need the two spreads to be equal.
    for bad in (
        None,
        "unknown",
        "missing_reference",
        "wrong_buffer",
        "underbuffer" if side == -1 else "wrong_raw",
    ):
        edits = {
            None: {},
            "unknown": {"stop_spread_policy": "unknown"},
            "missing_reference": {"stop_reference_ask": None},
            "wrong_buffer": {"stop_spread_buffer_price": 0.12},
            "underbuffer": {"send_ask": 2000.07},
            "wrong_raw": {"raw_chart_sl": 1900},
        }[bad]
        (folder / "demo.jsonl").write_text(
            "".join(json.dumps(v | edits if v is e else v) + "\n" for v in logs)
        )
        with closing(db_open(":memory:")) as db:
            sync(db, broker, broker.at, repo=tmp_path)
            data = make_views(db)[0]
            [t] = data["tickets"]
            assert t["net_profit"] == "10.00"
            if bad is None:
                assert (
                    float(t["initial_sl"]) == initial
                    and float(t["frozen_cross_sl"]) == r.raw_sl
                )
                assert len(data["setups"]) == 1 and t["trigger_timeframe"] == "M4"
            else:
                assert not t["frozen_cross_sl"] and not data["setups"]


@pytest.mark.parametrize("risk", [False, True])
def test_narrower_unsent_market_reprice_is_not_safety_bypass(hull_desk, risk):
    mt, d, _ = hull_desk("medium")
    first = mt.q
    moved = NS(time_msc=first.time_msc + 20, bid=first.bid, ask=2000.099)
    checks = []

    def check(req):
        checks.append(dict(req))
        if len(checks) == 1:
            mt.q = moved
        elif risk:
            d.lim = d.lim.model_copy(update={"max_open_risk_usd": 0.01})
        return NS(retcode=0 if len(checks) == 1 or risk else 1, margin_free=8900.0)

    mt.order_check = check
    assert d.batch(NAME, sig(2000.55, side=-1)) == (
        "portfolio_risk_limit" if risk else "broker_precheck_rejected"
    )
    assert (
        [q["sl"] for q in checks] == [2000.650, 2000.649]
        and not mt.requests
        and not d.state.get("manual_hold")
    )


def test_verified_broker_stop_is_kept_during_tp_realignment_and_sets_midpoint(
    hull_desk,
):
    mt, d, _, r = market(hull_desk, -1)
    mt.retprice = 100.02
    send = mt.order_send
    observed = 111.999999999  # inside the broker read-back tolerance of planned 112

    def slight_readback(req):
        result = send(req)
        if req["action"] == mt.TRADE_ACTION_DEAL and "position" not in req:
            mt.positions[-1].sl = observed
        return result

    mt.order_send = slight_readback
    assert d.batch("US30m", r) == "demo_batch_sent"
    align = [q for q in mt.requests if q["action"] == mt.TRADE_ACTION_SLTP]
    assert len(align) == 4 and all(
        (q["sl"], q["tp"]) == (observed, 90.02) for q in align
    )
    p = mt.positions[0]
    mt.positions[:] = [p]
    mt.requests.clear()
    rec = d.ladders.book[str(p.ticket)]
    assert rec["initial_sl"] == rec["sl"] == observed
    sample(mt, d, -1, 98)
    sample(mt, d, -1, 100.02)
    # Using the requested SL instead of the verified original would round to 106.01.
    assert (p.sl, p.tp) == (106, 90.02) and rec["initial_sl"] == observed


def test_market_last_quote_wider_than_checked_buffer_is_not_sent(hull_desk):
    mt, d, _ = hull_desk("medium")
    first = mt.q
    moved = NS(time_msc=first.time_msc + 20, bid=first.bid, ask=2000.101)
    qs = iter([first, first])
    mt.symbol_info_tick = lambda _: next(qs, moved)
    assert d.batch(NAME, sig(2000.55, side=-1)) == "final_quote_or_safety_changed"
    assert not mt.requests and not d.state.get("manual_hold")


def test_limit_last_quote_can_still_fit_the_tick_rounded_buffer(tmp_path, monkeypatch):
    mt, d, r, _, _ = retest(tmp_path, monkeypatch)
    mt.tick(2000, spread=0.061)  # tick=.01; actual room is .07, not .061
    check = mt.order_check

    def change(req):
        res = check(req)
        mt.tick(2000, spread=0.07)
        return res

    mt.order_check = change
    assert d.batch(NAME, r) == "retest_batch_placed"
    assert len(mt.requests) == 10 and all(q["sl"] == 2002.19 for q in mt.requests)


def test_locked_better_price_cannot_silently_replace_solver_with_less_cash():
    grid = Grid(0.01, 0.01, 2, 0, 0.01, 100, 0.01)

    def inconsistent_cash(side, lots, entry, mark):
        return 4.0 if mark == 93 else side * lots * (mark - entry)

    # The solver finds 94 for $6; the existing 93 can't be loosened, but its
    # inconsistent $4 broker valuation doesn't authorize a bare TP extension.
    with pytest.raises(Block, match="retained_level_below_cash_floor"):
        propose(grid, -1, 1, 100, 10, Protection(0, 93, 90), 92.5, inconsistent_cash)
