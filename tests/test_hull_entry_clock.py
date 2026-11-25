"""First-tick regression tests. All prices, quotes, terminals and clocks here are fixtures."""

import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_mt import T, sig

from bot.hull.config import Limits
from bot.hull.entry_clock import WAITING, check_quote, poll_delay
from bot.hull.structure import Structure
from bot.hull.terminal import dispatch


@pytest.mark.parametrize(
    "now,stamp,bid,ask,reason,wait",
    [
        (0, 0, 1.0, 1.0, "ready", False),
        (8, 4000, 1.0, 1.0, "ready", False),
        (8.001, 8000, 1.0, 1.0, "entry_window_expired", False),
        (0.200, -100, 1.0, 1.0, "quote_precedes_signal_close", True),
        (5.1, 1000, 1.0, 1.0, "quote_too_old", True),
        (8, -1, 1.0, 1.0, "quote_precedes_signal_close", False),
        (0.25, None, None, None, "quote_missing", True),
        (0.25, None, 1.0, 1.0, "invalid_quote", False),
        (0.25, 900, 1.0, 1.0, "ready", False),
        (0.25, 2251, 1.0, 1.0, "quote_clock_ahead", False),
        (0.25, 200, float("nan"), 1.0, "invalid_quote", False),
        (0.25, 200, 1.001, 1.0, "invalid_quote", False),
        (-0.25, -300, 1.0, 1.0, "signal_clock_ahead", False),
    ],
)
def test_tick_clock_reasons_and_closed_window_edges(now, stamp, bid, ask, reason, wait):
    sample = check_quote(
        T, T + now, Limits(), T * 1000 + stamp if stamp is not None else None, bid, ask
    )
    assert (sample.reason, sample.waitable) == (reason, wait)
    fields = sample.audit(1)
    json.dumps(fields, allow_nan=False)
    assert fields["signal_age_ms"] == pytest.approx(now * 1000, abs=0.002)
    if stamp is not None:
        assert fields["quote_age_ms"] == pytest.approx(now * 1000 - stamp, abs=0.002)


def test_polling_approaches_boundary_and_does_not_wait_on_a_symbol():
    assert T % 300 == 0
    assert poll_delay(T - 0.2, (1, 2, 5), 10, 8, False) == pytest.approx(0.2)
    assert poll_delay(T + 0.1, (2,), 10, 8, False) == 0.25
    assert poll_delay(T + 30, (2,), 10, 8, False) == 10
    assert poll_delay(T + 30, (2,), 10, 8, True) == 0.25


def test_restart_cannot_recover_wait_and_monotonic_deadline_does_not_slide(
    hull_desk, monkeypatch
):
    mt, d, _ = hull_desk()
    mt.q = None
    wall = [T + 0.5]
    mono = [500.0]
    monkeypatch.setattr("bot.hull.desk.time.time", lambda: wall[0])
    monkeypatch.setattr("bot.hull.desk.time.monotonic", lambda: mono[0])
    assert d.batch("XAUUSDm", sig()) == WAITING and not mt.requests
    assert d.batch("XAUUSDm", sig()) == "already_consumed"
    _, other, _ = hull_desk(state=json.loads(d.path.read_text()), mt=mt)
    assert not other.waiting and other.batch("XAUUSDm", sig()) == "already_consumed"
    # Even a stuck wall clock cannot give this reservation another window.
    mono[0] += 8
    mt.q = NS(time_msc=T * 1000 + 400, bid=2000.0, ask=2000.1)
    assert d.resume_first_quote("XAUUSDm", 1) == "entry_window_expired"
    assert not d.waiting and not mt.requests


@pytest.mark.parametrize(
    "change,expected",
    [
        ("kill", "kill"),
        ("spread", "spread_cap"),
        ("history", "data_or_deal_history_unavailable"),
        ("data", "data_or_deal_history_unavailable"),
        ("rewarm", "first_quote_wait_rewarmed"),
        ("stop", "first_quote_wait_stopped"),
        ("unfunded", "insufficient_free_margin"),
        ("manual_hold", "manual_reconciliation_required"),
        ("busy", "same_side_batch_open"),
        ("pending_order", "pending_orders_present"),
        ("supersede", "first_quote_wait_superseded"),
        ("invalid_tick", "invalid_quote"),
        ("future_tick", "quote_clock_ahead"),
    ],
)
def test_first_quote_resumption_rechecks_guards_and_never_repeats_exit(
    hull_desk, monkeypatch, change, expected
):
    mt, d, logs = hull_desk()
    old = mt.q
    mt.q = None
    exits = []
    monkeypatch.setattr(d, "close_opposite", lambda n, r: exits.append(r.close_s) or 0)
    ok = {"data_ok": True, "history_ok": True, "rewarmed": False}
    name = "XAUUSDm"
    dispatch(d, name, [sig()], **ok)
    assert (name, 1) in d.waiting and not mt.requests and exits == [T]
    mt.q = old
    if change == "kill":
        d.kill.touch()
    elif change == "spread":
        mt.q.ask = 2001.0
    elif change == "history":
        ok["history_ok"] = False
    elif change == "data":
        ok["data_ok"] = False
    elif change == "rewarm":
        ok["rewarmed"] = True
    elif change == "unfunded":
        mt.a.margin_free = 0.0
    elif change == "manual_hold":
        d.state["manual_hold"] = {"existing": True}
    elif change == "busy":
        mt.positions.append(
            NS(
                symbol=name,
                magic=d.h.magic,
                type=0,
                ticket=55,
                time_msc=(T - 5) * 1000,
                price_open=2000,
                sl=1999.0,
                tp=2001.0,
                volume=0.01,
            )
        )
        assert d.owners.bind(mt.positions[-1], 1)
    elif change == "pending_order":
        mt.orders.append(1)
    elif change == "invalid_tick":
        mt.q.bid = 0
    elif change == "future_tick":
        mt.q.time_msc = (T + 5) * 1000
    if change == "stop":
        d.cancel_all_waits()
    elif change == "supersede":
        monkeypatch.setattr("bot.hull.desk.time.time", lambda: T + 60.1)
        dispatch(
            d,
            name,
            [replace(sig(side=-1), close_s=T + 60, structure=Structure())],
            **ok,
        )
    else:
        dispatch(d, name, [], **ok)
    final = [x for k, x in logs if k == "label_decision" and x["close_s"] == T]
    assert len(final) == 1 and final[0]["outcome"] == expected
    assert not d.waiting and not mt.requests
    assert exits == ([T, T + 60] if change == "supersede" else [T])
    dispatch(d, name, [], **ok)
    assert not mt.requests


@pytest.mark.parametrize("stage", ["pre_send", "final_send", "next_leg"])
def test_after_first_plan_a_preclose_tick_with_identical_prices_cannot_send_or_wait(
    hull_desk, stage
):
    mt, d, logs = hull_desk("medium")
    calls = 0
    current = mt.q

    def quote(_):
        nonlocal calls
        calls += 1
        # prepare, pre-send, final-send; then next leg's prepare
        n = {"pre_send": 2, "final_send": 3, "next_leg": 4}[stage]
        return (
            current
            if calls < n
            else NS(time_msc=T * 1000 - 100, bid=current.bid, ask=current.ask)
        )

    mt.symbol_info_tick = quote
    why = d.batch("XAUUSDm", sig())
    assert (
        why
        == ("protected_partial_no_topup:" if stage == "next_leg" else "")
        + "quote_precedes_signal_close"
    )
    assert len(mt.positions) == len(mt.requests) == int(stage == "next_leg")
    assert not d.waiting and not d.state.get("manual_hold")
    assert d.batch("XAUUSDm", sig()) == "already_consumed"
    rejected = [
        v for k, v in logs if k == "entry_quote_check" and v["disposition"] == "reject"
    ]
    assert len(rejected) == 1 and rejected[0]["stage"] == stage


@pytest.mark.parametrize(
    "lane", ["duplicate", "opposite_invalid", "same_higher", "opposite_valid"]
)
def test_late_native_lane_at_same_close_keeps_its_own_quote_window(
    hull_desk, monkeypatch, lane
):
    mt, d, log = hull_desk(armed=False)
    q = mt.q
    mt.q = None
    raw = []
    name = "XAUUSDm"
    monkeypatch.setattr(d, "close_opposite", lambda n, r: raw.append(r.tf) or 0)
    ok = {"data_ok": True, "history_ok": True, "rewarmed": False}
    dispatch(d, name, [sig()], **ok)
    assert (name, 1) in d.waiting
    mt.q = q
    other = {
        "duplicate": sig(),
        "opposite_invalid": replace(sig(side=-1), tf=2, structure=Structure()),
        "same_higher": replace(sig(), tf=5),
        "opposite_valid": replace(sig(2001, side=-1), tf=2),
    }[lane]
    dispatch(d, name, [other], **ok)
    outcomes = {v["tf"]: v["outcome"] for k, v in log if k == "label_decision"}
    assert outcomes[1] == "shadow_plan_only" and not d.waiting and not mt.requests
    if lane != "duplicate":
        assert outcomes[other.tf] == (
            "neutral_structure" if lane == "opposite_invalid" else "shadow_plan_only"
        )
    assert raw == ([1] if lane == "duplicate" else [1, other.tf])
    assert d.state["lane_consumed"][name]["1"] == T


@pytest.mark.parametrize(
    "after,expected",
    [
        ("missing", "entry_window_expired"),
        ("fresh", "shadow_plan_only"),
        ("reject", "broker_precheck_rejected"),
        ("uncertain", "uncertain_or_partial_fill_manual_hold"),
    ],
)
def test_only_quote_availability_can_be_revisited(
    hull_desk, monkeypatch, after, expected
):
    mt, d, log = hull_desk(armed=after not in ("fresh", "missing"))
    name = "XAUUSDm"
    now = [T + 5.5]
    monkeypatch.setattr("bot.hull.desk.time.time", lambda: now[0])
    mt.q = None
    checks = []

    def broker_check(req):
        checks.append(req)
        return NS(retcode=7 if after == "reject" else 0, margin_free=8900.0)

    mt.order_check = broker_check
    assert d.batch(name, sig()) == WAITING
    # A post-close tick can itself be too old. It cannot be used to price an entry.
    now[0] += 0.25
    mt.q = NS(time_msc=T * 1000 + 100, bid=2000.0, ask=2000.1)
    assert d.resume_first_quote(name, 1) == WAITING and not checks and not mt.requests
    if after == "missing":
        mt.q = None
        now[0] = T + 8.001
    else:
        now[0] += 0.25
        mt.q.time_msc = int(now[0] * 1000) - 50
        mt.bad = after == "uncertain"
    assert d.resume_first_quote(name, 1) == expected
    assert not d.waiting and d.batch(name, sig()) == "already_consumed"
    assert len(checks) == int(after in ("reject", "uncertain")) and len(
        mt.requests
    ) == int(after == "uncertain")
    assert [v["reason"] for k, v in log if k == "entry_quote_check"] == [
        "quote_missing",
        "quote_too_old",
        "entry_window_expired" if after == "missing" else "ready",
    ]
    if after == "uncertain":
        assert d.state["manual_hold"]["leg"] == 1


def test_waiting_symbol_does_not_hold_up_another_symbol(hull_desk):
    mt, d, log = hull_desk(armed=False)
    a, b = "XAUUSDm", "EURUSDm"
    d.syms[b] = (
        "EURUSD",
        d.syms[a][1],
    )  # identical synthetic grid for the independent lane
    ok = {"data_ok": True, "history_ok": True, "rewarmed": False}
    mt.symbol_info_tick = lambda n: None if n == a else mt.q
    dispatch(d, a, [sig()], **ok)
    dispatch(d, b, [sig()], **ok)
    assert any(
        e == "label_decision"
        and x["symbol"] == b
        and x["outcome"] == "shadow_plan_only"
        for e, x in log
    )
    assert list(d.waiting) == [(a, 1)] and d.state["lane_consumed"] == {
        a: {"1": T},
        b: {"1": T},
    }
    mt.symbol_info_tick = lambda n: mt.q
    dispatch(d, a, [], **ok)
    assert not d.waiting and not mt.requests


def test_countertrend_raw_cross_cannot_reserve_a_first_entry_quote(hull_desk):
    mt, d, _ = hull_desk(armed=False)
    mt.q = None
    r = replace(sig(), structure=Structure(bias=-1))
    dispatch(d, "XAUUSDm", [r], data_ok=True, history_ok=True, rewarmed=False)
    assert not d.waiting and not mt.requests
