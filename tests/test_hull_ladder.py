"""Real order-planning and supervision paths against a synthetic MT5 terminal only."""

import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_mt import T, scanner_clock, sig

from bot.broker import DemoSafetyError
from bot.hull.config import FIXED, LADDER
from bot.hull.ladder import Protection, percentages, propose, reached
from bot.hull.orders import Block, Grid, target

NAME = "XAUUSDm"


def filled(hull_desk, side=1, *, base=10.0, lots=0.1, single=True, slip=None):
    mt, d, log = hull_desk("medium")
    key, p = d.syms[NAME]
    d.syms[NAME] = (key, p.model_copy(update={"lots": lots, "target_usd": base}))
    mt.retprice = slip
    assert (
        d.batch(NAME, sig(sl=1999.6 if side == 1 else 2000.6, side=side))
        == "demo_batch_sent"
    )
    pos = mt.positions[0]
    if single:
        mt.positions[:] = [pos]  # other protected tickets have already closed
    mt.requests.clear()
    return mt, d, log, pos


def quote(mt, p, usd, *, stamp=None, spread=0.1):
    buy = p.type == 0
    side = 1 if buy else -1
    mark = round(p.price_open + side * usd / (100 * p.volume), 8)
    t = mt.q.time_msc + 1 if stamp is None else stamp
    mt.q = NS(
        time_msc=t,
        bid=mark if buy else round(mark - spread, 8),
        ask=round(mark + spread, 8) if buy else mark,
    )
    return mark


def log_for(log, k):
    return [v for e, v in log if e == k]


@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize("base,lots", [(10.0, 0.10), (25.0, 0.50)])
def test_each_rung_from_actual_fill_and_original_per_ticket_cash(
    hull_desk, side, base, lots
):
    fill = 2000.12 if side == 1 else 1999.98
    mt, d, log, p = filled(hull_desk, side, base=base, lots=lots, slip=fill)
    r = d.ladders.book[str(p.ticket)]
    assert r["configured_target_usd"] == base
    assert r["entry_spread_usd"] == pytest.approx(0.1 * lots * 100)
    base += 0.1 * lots * 100  # expected frozen goal, not (slipped fill - later mark)
    assert r["base_usd"] == pytest.approx(base) and r["entry"] == fill
    assert d.h.exit_policy == r["policy"] == LADDER and not d.state.get(
        "target_by_ticket"
    )
    initial = (p.sl, p.tp)
    p.profit = 99 * base  # stale, even above the original goal; NOT the trigger
    quote(mt, p, 0.699 * base)
    d.supervise()
    assert (p.sl, p.tp) == initial and not mt.requests
    for i in range(1, 9):
        trigger, lock, tp = percentages(i)
        quote(mt, p, base * trigger / 100)
        d.supervise()
        assert len(mt.positions) == 1 and len(mt.requests) == i
        assert mt.requests[-1] == {
            "action": mt.TRADE_ACTION_SLTP,
            "position": p.ticket,
            "symbol": NAME,
            "magic": d.h.magic,
            "sl": p.sl,
            "tp": p.tp,
        }
        assert (p.sl, p.tp) == pytest.approx(
            (
                fill + side * base * lock / (100 * 100 * lots),
                fill + side * base * tp / (100 * 100 * lots),
            )
        )
        assert r["rung"] == i and not r.get("pending")
        ev = log_for(log, "profit_ladder_step")[-1]
        assert (ev["trigger_pct"], ev["lock_pct"], ev["target_pct"]) == (
            trigger,
            lock,
            tp,
        )
        # Repeat the tick, pull back above the active SL, then repeat after journal reload.
        d.supervise()
        quote(mt, p, base * (trigger - 5) / 100)
        d.supervise()
        assert len(mt.requests) == i
    assert (
        json.loads(d.path.read_text())["ladder_by_ticket"][str(p.ticket)]["rung"] == 8
    )


@pytest.mark.parametrize("side", [1, -1])
def test_executable_side_and_already_crossed_tp_or_stop_wins(hull_desk, side):
    for usd in (11.0, 17.6, -20.0):
        mt, d, _, p = filled(hull_desk, side)
        quote(mt, p, 7.2, spread=0.2)
        d.supervise()
        assert not mt.requests  # wrong quote side exceeds 70%
        quote(mt, p, usd)
        d.supervise()
        assert not mt.requests and d.ladders.book[str(p.ticket)]["rung"] == 0
        quote(mt, p, 8.14)
        d.supervise()  # a delayed positions snapshot cannot rescue an already-crossed barrier
        assert not mt.requests and d.ladders.book[str(p.ticket)]["broker_exit_seen"]


@pytest.mark.parametrize(
    "usd,n",
    [(6.9999, 0), (7.0, 1), (9.9999, 1), (10.0, 2), (13.0, 3), (16.0, 4), (160.0, 52)],
)
def test_rung_boundaries(usd, n):
    assert reached(usd, 10) == n


def test_nonlinear_fx_conversion_tick_rounding_both_ways():
    g = Grid(0.001, 0.001, 3, 0, 0.01, 100, 0.01)
    cash = lambda side, lot, entry, px: side * lot * 100000 * (px - entry) / px
    for side in (1, -1):
        entry = 152.032
        mark = entry + side * 0.09
        # Profit about $7.1 / ticket, priced from the actual $10 initial TP.
        old = Protection(
            0, entry - side * 0.5, target(g, side, 0.12, entry, 10.0, cash)[0]
        )
        new = propose(g, side, 0.12, entry, 10.0, old, mark, cash)
        assert new and new.rung == 1
        for price, goal in [(new.sl, 6), (new.tp, 12)]:
            assert (
                cash(side, 0.12, entry, price) >= goal
                and cash(side, 0.12, entry, price - side * g.tick) < goal
            )
        with pytest.raises(Block, match="cash_conversion"):
            propose(g, side, 0.12, entry, 10.0, old, mark, lambda *a: None)


@pytest.mark.parametrize(
    "changed",
    [
        "no_tick",
        "stale",
        "future",
        "freeze",
        "manual",
        "external_sl",
        "different_lots",
        "identifier",
        "server",
        "real",
        "observer",
        "unadopted",
        "conversion",
    ],
)
def test_fail_closed_without_loosening_or_adopting_positions(hull_desk, changed):
    mt, d, log, p = filled(hull_desk)
    r = d.ladders.book[str(p.ticket)]
    before = p.sl, p.tp
    quote(mt, p, 7.7)
    if changed == "no_tick":
        mt.q = None
    elif changed == "stale":
        mt.q.time_msc -= 5000
    elif changed == "future":
        mt.q.time_msc += 5000
    elif changed == "freeze":
        mt.i.trade_freeze_level = 201  # desired lock only .10 behind bid
    elif changed == "manual":
        p.magic = 77
    elif changed == "external_sl":
        p.sl += 0.02
        before = p.sl, p.tp
    elif changed == "different_lots":
        p.volume *= 0.5
    elif changed == "identifier":
        p.identifier = 999
    elif changed == "server":
        mt.a.server = "other"
    elif changed == "real":
        mt.a.trade_mode = 2
    elif changed == "observer":
        d.armed = False
    elif changed == "unadopted":
        d.state.pop("ladder_by_ticket")
    elif changed == "conversion":
        mt.order_calc_profit = lambda *a: None
    if changed in ("real", "server"):
        with pytest.raises(DemoSafetyError):
            d.supervise()
    else:
        d.supervise()
    assert not mt.requests and (p.sl, p.tp) == before and r["rung"] == 0
    if changed in ("external_sl", "different_lots", "identifier"):
        assert r["hold"] and d.ladders.needs_review()
    if changed == "freeze":
        assert (
            log_for(log, "profit_ladder_deferred")[-1]["reason"]
            == "ladder_stops_or_freeze_distance"
        )


@pytest.mark.parametrize(
    "case", ["price_retreat", "old_tp", "stale", "ticket_closed", "kill", "order_check"]
)
def test_recheck_before_modification_never_sends_the_old_quote_plan(hull_desk, case):
    mt, d, _, p = filled(hull_desk)
    quote(mt, p, 7.82)
    old = p.sl, p.tp

    def checked(req):
        if case == "price_retreat":
            quote(mt, p, 6.7)
        elif case == "old_tp":
            quote(mt, p, 11.6)
        elif case == "stale":
            mt.q.time_msc -= 5000
        elif case == "ticket_closed":
            mt.positions.clear()
        elif case == "kill":
            d.kill.touch()
        return NS(retcode=9 if case == "order_check" else 0)

    mt.order_check = checked
    d.supervise()
    assert (
        not mt.requests
        and (p.sl, p.tp) == old
        and "sends" not in d.state
        and not d.ladders.needs_review()
    )


def test_persist_and_reconcile_uncertain_amendment_never_resend(hull_desk):
    mt, d, _, p = filled(hull_desk)
    quote(mt, p, 7.7)
    key = str(p.ticket)

    def lose_ack(req):
        snap = json.loads(d.path.read_text())["ladder_by_ticket"][key]
        assert (
            snap["rung"] == 0 and snap["pending"]["rung"] == 1
        )  # durable BEFORE touching broker
        mt.requests.append(dict(req))

    mt.order_send = lose_ack
    d.supervise()
    d.supervise()
    assert len(mt.requests) == 1 and d.ladders.needs_review()
    _, d2, log = hull_desk(mt=mt, state=json.loads(d.path.read_text()))
    d2.h = d2.h.model_copy(update={"exit_policy": FIXED})
    quote(mt, p, 8)
    d2.supervise()
    assert len(mt.requests) == 1 and d2.ladders.needs_review()
    assert (
        d2.batch(NAME, replace(sig(), close_s=T + 1))
        == "ladder_reconciliation_required"
    )
    # Delayed ACK: observed pair on the SAME ticket is authoritative, even after restart/config change.
    req = mt.requests[0]
    p.sl, p.tp = req["sl"], req["tp"]
    d2.supervise()
    assert (
        d2.ladders.book[key]["rung"] == 1
        and not d2.ladders.needs_review()
        and len(mt.requests) == 1
    )
    assert len(log_for(log, "profit_ladder_step")) == 1
    d2.supervise()
    assert len(mt.requests) == 1


def test_fixed_legacy_and_new_tickets_can_coexist(hull_desk):
    mt, d, _, p = filled(hull_desk)
    legacy = NS(**{**vars(p), "ticket": 991, "profit": 10.0})
    manual = NS(**{**vars(p), "ticket": 992, "magic": 77, "profit": 100.0})
    mt.positions.extend([legacy, manual])
    d.state["target_by_ticket"] = {str(legacy.ticket): 10.0}
    quote(mt, p, 8.14)
    d.supervise()
    assert [a["action"] for a in mt.requests] == [
        mt.TRADE_ACTION_SLTP,
        mt.TRADE_ACTION_DEAL,
    ]
    assert [a["position"] for a in mt.requests] == [
        p.ticket,
        legacy.ticket,
    ] and manual in mt.positions
    quote(mt, p, 11.0)
    p.profit = 11.0
    d.supervise()
    assert p in mt.positions and d.ladders.book[str(p.ticket)]["rung"] == 2
    from bot.hull.curves import LiveTick

    opposite = replace(sig(side=-1), trigger=LiveTick.read(mt.q), close_s=T + 60)
    assert d.close_opposite(NAME, opposite) == 1 and mt.positions == [manual]
    d.supervise()
    assert not d.ladders.book and not d.state["target_by_ticket"]


def test_all_ladders_advance_beyond_40_amendments_on_fresh_ticks_in_one_second(
    hull_desk,
):
    mt, d, log, p = filled(hull_desk, single=False)
    d.state["sends"] = [T] * 40  # left by an earlier build; never read or extended
    for rung in range(1, 12):
        quote(mt, p, 7.75 + 3.3 * (rung - 1))
        d.supervise()
        assert all(r["rung"] == rung for r in d.ladders.book.values())
    assert len(mt.requests) == len(log_for(log, "profit_ladder_step")) == 44
    assert all(
        r["action"] == mt.TRADE_ACTION_SLTP and r["tp"] > r["sl"] > 0
        for r in mt.requests
    )
    assert d.state["sends"] == [T] * 40
    d.supervise()  # same tick: no duplicate amendments
    assert len(mt.requests) == 44


@pytest.mark.parametrize(
    "code, label",
    [(10029, "TRADE_RETCODE_FROZEN"), (10024, "TRADE_RETCODE_TOO_MANY_REQUESTS")],
)
def test_definite_broker_refusal_never_changes_local_protection_and_can_recheck(
    hull_desk, monkeypatch, code, label
):
    mt, d, log, p = filled(hull_desk)
    key = str(p.ticket)
    old = p.sl, p.tp
    setattr(mt, label, code)
    original = mt.order_send
    mono = [50.0]
    monkeypatch.setattr("bot.hull.desk.time.monotonic", lambda: mono[0])
    mt.order_send = lambda req: mt.requests.append(dict(req)) or NS(retcode=code)
    quote(mt, p, 7.8)
    d.supervise()
    assert len(mt.requests) == 1 and (p.sl, p.tp) == old
    assert d.ladders.book[key]["rung"] == 0 and not d.ladders.needs_review()
    assert (
        log_for(log, "profit_ladder_deferred")[-1]["reason"] == "ladder_broker_refused"
    )
    quote(mt, p, 7.9)
    d.supervise()
    assert len(mt.requests) == 1
    mt.order_send = original
    mono[0] += 1.1
    quote(mt, p, 8.0)
    d.supervise()
    assert d.ladders.book[key]["rung"] == 1 and len(mt.requests) == 2


def test_new_default_for_old_yaml_and_default_does_not_adopt_any_open_ticket(hull_desk):
    from hull_mt import ROOT

    from bot.config.loader import load_yaml
    from bot.hull.config import HullConfig

    old_yaml = load_yaml(ROOT / "config.yaml")["hull"]
    old_yaml.pop("exit_policy")
    assert HullConfig.model_validate(old_yaml).exit_policy == LADDER
    mt, d, _, p = filled(hull_desk)
    old = p.sl, p.tp
    # No record in the journal means this older ticket is NOT enrolled by the upgrade.
    del d.state["ladder_by_ticket"]
    quote(mt, p, 7.8)
    d.supervise()
    assert (p.sl, p.tp) == old and not mt.requests


def test_tick_supervisor_runs_between_native_bar_closes_on_resumed_tickets(
    hull_desk, tmp_path, monkeypatch
):
    import hashlib
    import sys

    from hull_mt import ROOT

    from bot.config.loader import load_yaml
    from bot.hull.config import selection
    from bot.hull.terminal import run

    mt, d, _, p = filled(hull_desk)
    key = hashlib.sha256(repr((1, "t")).encode()).hexdigest()[:16]
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["run"]["mode"] = "demo"
    home = tmp_path / "desktop"
    private = home / ".moneyprinterbot/hull" / key
    private.mkdir(parents=True)
    (private / "state.json").write_text(json.dumps(d.state))
    mt.initialize = lambda: True
    mt.shutdown = lambda: None
    mt.history_deals_get = lambda *_: []
    mt.copy_rates_from_pos = lambda name, tf, shift, n: [
        {
            "time": T - 60 * (100 - i),
            "open": 2000.0,
            "high": 2000.1,
            "low": 1999.9,
            "close": 2000.0,
        }
        for i in range(100)
    ][-n:]
    clock = [T + 30.0]
    mono = [450.0]
    samples = [7.7, 11.0, 14.3]
    sleeps = []
    quote(mt, p, samples[0], stamp=int(clock[0] * 1000) - 1)

    def advance(sec):
        sleeps.append(sec)
        clock[0] += sec
        mono[0] += sec
        quote(mt, p, samples[len(sleeps)], stamp=int(clock[0] * 1000) - 1)

    monkeypatch.setitem(sys.modules, "MetaTrader5", mt)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    scanner_clock(monkeypatch, lambda: clock[0], advance, lambda: mono[0])
    run(
        cfg,
        d.h,
        selection(d.h, profiles=["XAUUSD:0.10:10:medium"]),
        (1,),
        armed=True,
        poll=10,
        iterations=3,
    )
    events = [
        json.loads(l)
        for l in (tmp_path / f"reports/hull_runtime/{key}/demo.jsonl")
        .read_text()
        .splitlines()
    ]
    assert sleeps == [0.25, 0.25] and not any(
        e["event"] == "closed_label" for e in events
    )
    assert [e["rung"] for e in events if e["event"] == "profit_ladder_step"] == [
        1,
        2,
        3,
    ]
    assert all(req["action"] == mt.TRADE_ACTION_SLTP for req in mt.requests)
    summary = events[0]["summary"]
    assert summary["new_ticket_exit_policy"] == LADDER
    assert summary["new_ticket_target_policy"] == "base_plus_entry_spread_v1"
    for view in summary["specs"][NAME]["target_preview_by_side"].values():
        assert view["initial_goal_usd"] == pytest.approx(11)
        assert view["ladder_preview_per_ticket"][2] == pytest.approx(
            {"rung": 3, "trigger_usd": 14.3, "lock_usd": 13.2, "target_usd": 19.8}
        )


def test_dont_enroll_a_different_ticket_from_same_snapshot(hull_desk):
    mt, d, _ = hull_desk()
    original = mt.order_send

    def inconsistent(req):
        res = original(req)
        res.order = 99999
        return res

    mt.order_send = inconsistent
    assert d.batch(NAME, sig()) == "opening_order_not_reconciled_manual_hold"
    assert (
        not d.ladders.book
        and len(mt.requests) == 1
        and mt.positions[0].sl > 0
        and mt.positions[0].tp > 0
    )
