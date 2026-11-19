"""First-tick regression tests. All prices, quotes, terminals and clocks here are fixtures."""

import json
import math
import sys
from dataclasses import replace
from datetime import UTC, datetime
from itertools import pairwise
from types import SimpleNamespace as NS

import pytest
from hull_mt import MT, ROOT, T, scanner_clock, sig

from bot.config.loader import load_yaml
from bot.hull.config import HullConfig, Limits, selection
from bot.hull.entry_clock import QUOTE_POLICY, WAITING, check_quote, poll_delay
from bot.hull.signal import Hull
from bot.hull.structure import Structure
from bot.hull.terminal import dispatch, run
from bot.zones.engine import Bar


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
        (0.25, 251, 1.0, 1.0, "quote_clock_ahead", False),
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
    assert d.resume_first_quote("XAUUSDm") == "entry_window_expired"
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
    assert name in d.waiting and not mt.requests and exits == [T]
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
                price_open=2000,
                sl=1999.0,
                tp=2001.0,
                volume=0.01,
            )
        )
    elif change == "pending_order":
        mt.orders.append(1)
    elif change == "invalid_tick":
        mt.q.bid = 0
    elif change == "future_tick":
        mt.q.time_msc = (T + 1) * 1000
    if change == "stop":
        d.cancel_all_waits()
    elif change == "supersede":
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


def eurusd_bars(end):
    """Synthetic M1 tape: legacy Hull 24/hl2, 3/3 pivots. NOT the incident's broker history."""
    anchors = [
        (0, 120),
        (5, 122),
        (11, 114.8),
        (16, 118),
        (22, 111.4),
        (27, 115),
        (33, 108),
        (38, 112),
        (44, 103),
        (50, 107),
        (56, 98),
        (62, 102.4),
        (68, 95),
        (80, 109.65),
        (90, 100.40),
        (96, 102.9),
        (100, 104.1),
        (104, 103.55),
        (108, 105.2),
        (119, 115.65),
        (124, 114.6),
    ]
    cc = []
    for (a, v), (b, w) in pairwise(anchors):
        for i in range(a, b):
            f = (i - a) / (b - a)
            cc.append(
                round(
                    v + (w - v) * f + 0.1 * math.sin(i * 2.4) * math.sin(math.pi * f), 2
                )
            )

    def fx(n):
        return round(1.1077 - n * 0.0001, 5)

    bars = []
    for i, c in enumerate(cc):
        o = cc[i - 1] if i else c + 0.2
        h = round(max(o, c) + 0.08 + 0.11 * ((i * 7) % 9) / 8, 2)
        l = round(min(o, c) - 0.08 - 0.09 * ((i * 5) % 11) / 10, 2)
        if i == 80:
            h = 110.0
        if i == 90:
            l = 100.0
        if i == 119:
            h = 116.0
        bars.append(Bar(end - 60 * (len(cc) - i), fx(o), fx(l), fx(h), fx(c)))
    return bars


@pytest.mark.parametrize(
    "case,armed,outcome",
    [
        ("fresh", True, "demo_batch_sent"),
        ("fresh", False, "shadow_plan_only"),
        ("late", True, "entry_window_expired"),
        ("outside", True, "quote_outside_buy_below_bear_bos"),
    ],
)
def test_eurusd_1952_bar_1953_close_waits_for_quote_not_replayed(
    case, armed, outcome, tmp_path, monkeypatch
):
    symbol = "EURUSDm"
    end = int(datetime(2026, 11, 18, 19, 53, tzinfo=UTC).timestamp())
    bars = eurusd_bars(end)
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["hull"]["indicator"].update(
        hull_len=24, source="hl2"
    )  # pinned incident fixture
    cfg["run"]["mode"] = "demo" if armed else "paper"
    h = HullConfig.model_validate(cfg["hull"])
    eng = Hull(1, h.indicator, 0.0001)
    r = [eng.update(b) for b in bars][-1]
    assert (
        r.close_s == end
        and r.entry_side == 1
        and r.entry_audit()["entry_setup"] == "buy_below_bear_bos"
    )
    mt = MT()
    mt.i.name = symbol
    mt.i.trade_tick_size = mt.i.point = 0.00001
    mt.i.digits = 5
    mt.i.trade_contract_size = 100000
    mt.initialize = lambda: True
    mt.shutdown = lambda: None
    mt.history_deals_get = lambda *_: []
    mt.order_calc_profit = lambda side, n, lot, start, px: (
        (1 if side == 0 else -1) * lot * 100000 * (px - start)
    )
    wall = [end - 0.25]
    mono = [300.0]
    sleeps = []

    def quote(n):
        assert n == symbol
        if wall[0] < end + 0.70:
            ms = end * 1000 - 100
        else:
            ms = int((wall[0] - 0.05) * 1000)
        ask = (
            r.structure.broken.price
            if case == "outside" and end + 0.70 <= wall[0] < end + 1.0
            else round(r.close + 0.00005, 5)
        )
        return NS(time_msc=ms, bid=round(ask - 0.00005, 5), ask=ask)

    def rates(n, tf, shift, count):
        assert n == symbol and tf == 1 and shift == 1
        return [
            {"time": b.t, "open": b.o, "high": b.h, "low": b.l, "close": b.c}
            for b in bars
            if b.t + 60 <= wall[0]
        ][-count:]

    def advance(dt):
        sleeps.append(dt)
        actual = 9.0 if case == "late" and wall[0] >= end else dt
        wall[0] += actual
        mono[0] += actual

    mt.symbol_info_tick = quote
    mt.copy_rates_from_pos = rates
    scanner_clock(monkeypatch, lambda: wall[0], advance, lambda: mono[0])
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    run(
        cfg,
        h,
        selection(h, profiles=["EURUSD:0.01:10:slow"]),
        (1,),
        armed=armed,
        poll=10.0,
        iterations=6,
    )
    records = [
        json.loads(v)
        for v in next(tmp_path.glob("reports/hull_runtime/*/*.jsonl"))
        .read_text()
        .splitlines()
    ]
    summary = records[0]["summary"]
    assert (
        summary["entry_policy"] == "protected_swing_v2"
        and summary["entry_quote_policy"] == QUOTE_POLICY
    )
    (label,) = [v for v in records if v["event"] == "closed_label"]
    assert (
        label["bar_open_utc"].endswith("19:52:00.000+00:00")
        and label["signal_close_utc"].endswith("19:53:00.000+00:00")
        and label["entry_side"] == 1
    )
    timing = [v for v in records if v["event"] == "entry_quote_check"]
    first = timing[0]
    assert (
        first["reason"] == "quote_precedes_signal_close"
        and first["tick_time_msc"] == end * 1000 - 100
        and first["disposition"] == "wait"
    )
    (decision,) = [v for v in records if v["event"] == "label_decision"]
    assert decision["outcome"] == outcome and decision["close_s"] == end
    assert all(dt <= 0.25 for dt in sleeps[:2])
    assert timing[-1]["reason"] == (
        "entry_window_expired" if case == "late" else "ready"
    )
    if outcome == "demo_batch_sent":
        assert (
            timing[-1]["signal_age_ms"] == 750
            and len(mt.positions) == len(mt.requests) == 10
        )
        assert all(
            p.type == mt.POSITION_TYPE_BUY
            and p.volume == 0.01
            and p.sl < p.price_open < p.tp
            for p in mt.positions
        )
    else:
        assert not mt.requests and not mt.positions


@pytest.mark.parametrize(
    "lane,outcome,chosen,exits",
    [
        ("duplicate", "shadow_plan_only", 1, [1]),
        ("opposite_invalid", "shadow_plan_only", 1, [1, 2]),
        ("same_higher", "shadow_plan_only", 5, [1, 5]),
        ("opposite_valid", "opposite_labels_same_close", 1, [1, 2]),
    ],
)
def test_late_native_lane_at_same_close_does_not_reset_a_quote_window(
    hull_desk, monkeypatch, lane, outcome, chosen, exits
):
    mt, d, log = hull_desk(armed=False)
    q = mt.q
    mt.q = None
    raw = []
    name = "XAUUSDm"
    monkeypatch.setattr(d, "close_opposite", lambda n, r: raw.append(r.tf) or 0)
    ok = {"data_ok": True, "history_ok": True, "rewarmed": False}
    dispatch(d, name, [sig()], **ok)
    assert name in d.waiting
    mt.q = q
    other = {
        "duplicate": sig(),
        "opposite_invalid": replace(sig(side=-1), tf=2, structure=Structure()),
        "same_higher": replace(sig(), tf=5),
        "opposite_valid": replace(sig(side=-1), tf=2),
    }[lane]
    dispatch(d, name, [other], **ok)
    (done,) = [v for k, v in log if k == "label_decision"]
    assert done["tf"] == chosen and done["outcome"] == outcome and not d.waiting
    assert not mt.requests and raw == exits and d.state["consumed"][name] == T


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
    assert d.resume_first_quote(name) == WAITING and not checks and not mt.requests
    if after == "missing":
        mt.q = None
        now[0] = T + 8.001
    else:
        now[0] += 0.25
        mt.q.time_msc = int(now[0] * 1000) - 50
        mt.bad = after == "uncertain"
    assert d.resume_first_quote(name) == expected
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
    mt, d, _ = hull_desk(armed=False)
    a, b = "XAUUSDm", "SECOND"
    d.syms[b] = d.syms[a]
    ok = {"data_ok": True, "history_ok": True, "rewarmed": False}
    mt.symbol_info_tick = lambda n: None if n == a else mt.q
    dispatch(d, a, [sig()], **ok)
    dispatch(d, b, [sig()], **ok)
    assert list(d.waiting) == [a] and d.state["consumed"] == {a: T, b: T}
    mt.symbol_info_tick = lambda n: mt.q
    dispatch(d, a, [], **ok)
    assert not d.waiting and not mt.requests
