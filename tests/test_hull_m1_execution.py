"""Two clocks, one owner. Real production scanner with native synthetic bid bars and a fake MT5."""

import json
import sys
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_cross_tape import as_rate
from hull_mt import MT, ROOT, T, scanner_clock
from test_hull_failed_mss import P, bars

from bot.config.loader import load_yaml
from bot.hull.bar import Bar
from bot.hull.config import M1_MODE, M1_STOP, HullConfig, selection
from bot.hull.curves import LiveTick
from bot.hull.feed import Stream
from bot.hull.minute import candidate, choose
from bot.hull.orders import cross_candle_stop
from bot.hull.signal import FirstCrossGate
from bot.hull.structure import Structure
from bot.hull.terminal import run

NAME = "XAUUSDm"
S = T + 120  # two minutes into all four parent bars, not a parent close


def tape(
    tmp_path, mp, side=1, frames=(5, 30), case="entry", armed=True, preflight=False
):
    mt = MT()
    times = [S - 0.25, S + 0.125, S + 0.375, S + 0.625]
    phase = [0]
    last = [False]
    calls = []

    def wall():
        return times[phase[0]]

    def price():
        if case == "flicker":
            return 2000.0 if phase[0] == 1 else 2000.0 - 2 * side
        return (
            2000.0 - 3 * side
            if ((case == "exit" and phase[0] >= 2) or (case == "interrupt" and last[0]))
            else 2000.0
        )

    if case == "flicker":
        times[:] = [S - 0.8, S - 0.25, S + 0.125, S + 0.4]
    if case == "late":
        times[1:] = [S + 9.125, S + 9.375, S + 9.625]
    mt.i.trade_tick_size = 0.025
    minute = bars(side, 1, end=S)
    parents = {tf: bars(side, tf, end=T) for tf in frames}
    if 45 in frames:
        parents[15] = [
            b
            for x in parents[45]
            for b in (
                Bar(x.t, x.o, x.h, x.l, x.o),
                Bar(x.t + 900, x.o, x.o, x.o, x.o),
                Bar(x.t + 1800, x.o, max(x.o, x.c), min(x.o, x.c), x.c),
            )
        ]

    def history(name, period, pos, count):
        calls.append((period, pos, count, phase[0]))
        if case == "missing" and period == max(frames):
            return []
        if period == 1:
            before = wall() < S
            x = minute[:-1] if before else minute
            c = price()
            if case == "flicker" and not before:
                x = [*minute[:-1], Bar(S - 60, 2000, max(2000, c), min(2000, c), c)]
            op = S - 60 if before else S
            form = Bar(op, 2000, max(2000, c), min(2000, c), c)
        else:
            x = parents[period]
            c = price()
            form = Bar(T, 2000, max(c, 2000), min(c, 2000), c)
        return [as_rate(b) for b in (x if pos else [*x, form])[-count:]]

    mt.copy_rates_from_pos = history

    def days(n, period, start, end):
        assert period == mt.TIMEFRAME_D1
        a = int(start.timestamp())
        b = int(end.timestamp())
        return [{"time": x} for x in range(a // 86400 * 86400, b + 1, 86400) if x >= a]

    mt.copy_rates_range = days

    def tick(_n):
        ms = int(wall() * 1000)
        c = price()
        if case == "wait" and phase[0] == 1:
            ms = S * 1000 - 1
        mt.q = NS(time_msc=ms, bid=c, ask=c + (0.75 if case == "spread" else 0.05))
        return mt.q

    mt.symbol_info_tick = tick
    mt.initialize = lambda: True
    mt.shutdown = lambda: None
    send = mt.order_send

    def submit(req):
        ret = send(req)
        if case == "interrupt" and "position" not in req:
            last[0] = True
        return ret

    mt.order_send = submit
    mp.chdir(tmp_path)
    mp.setenv("HOME", str(tmp_path))
    mp.setenv("USERPROFILE", str(tmp_path))
    mp.setitem(sys.modules, "MetaTrader5", mt)
    scanner_clock(mp, wall, lambda _: phase.__setitem__(0, phase[0] + 1))
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["hull"].update(
        execution_mode=M1_MODE, eligible_timeframes=[f"M{x}" for x in frames]
    )
    cfg["hull"]["indicator"]["swing_len"] = 1
    cfg["run"]["mode"] = "demo"
    h = HullConfig.model_validate(cfg["hull"])
    summary = run(
        cfg,
        h,
        selection(h, rate="medium"),
        armed=armed,
        preflight=preflight,
        iterations=len(times),
    )
    if preflight:
        return mt, summary, minute, calls
    log = next((tmp_path / "reports" / "hull_runtime").glob("*/*.jsonl"))
    rows = [json.loads(l) for l in log.read_text().splitlines()]
    return mt, rows, minute, calls


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("frames", [(5, 30), (15,), (45,), (5, 15, 30, 45)])
def test_real_m1_trigger_inside_parent_without_parent_cross(
    tmp_path, monkeypatch, side, frames
):
    mt, rows, candles, calls = tape(tmp_path, monkeypatch, side, frames)
    assert len(mt.positions) == 4 and len(mt.requests) == 4
    start = rows[0]["summary"]
    assert (
        start["trigger_timeframe"] == "M1"
        and start["timeframes"] == [f"M{x}" for x in frames]
        and start["specs"][NAME]["max_tickets_across_selected_lanes"] == 4
    )
    receipts = [r for r in rows if r["event"] == "demo_result"]
    assert len(receipts) == 4
    for r in receipts:
        assert r["tf"] == max(frames)
        assert (
            r["parent_tf"] == max(frames)
            and r["parent_structure_close_s"] == T
            and r["trigger_tf"] == 1
        )
        assert (
            r["signal"] == S
            and r["cross_candle_open_s"] == S - 60
            and r["initial_stop_policy"] == M1_STOP
        )
        wick = candles[-1].l if side == 1 else candles[-1].h
        assert r["cross_candle_wick"] == wick and r["request"][
            "sl"
        ] == cross_candle_stop(wick, side, r["cross_atr"] * 0.1, 0.025)
        assert (
            r["request"]["comment"] == f"m{max(frames)}_{S}_{r['leg']}"
            and r["request"]["deviation"] == 0
        )
        assert (
            r["close_to_detect_ms"] == r["close_to_submit_ms"] == 125
            and r["order_send_roundtrip_ms"] >= 0
        )
    assert all(req["sl"] > 0 and req["tp"] > 0 for req in mt.requests)
    assert not any(
        p != 1 and pos == 1 and phase > 0 for p, pos, count, phase in calls
    )  # closed parents were cached inside their candle
    assert (
        len(
            [
                r
                for r in rows
                if r["event"] == "m1_arbitration" and r["outcome"] == "selected"
            ]
        )
        == 1
    )


@pytest.mark.parametrize(
    "case,legs,outcome",
    [
        ("late", 0, "entry_window_expired"),
        ("spread", 0, "spread_cap"),
        ("wait", 4, "demo_batch_sent"),
        (
            "interrupt",
            1,
            "protected_partial_no_topup:next_bar_quote_or_context_unverified",
        ),
    ],
)
def test_first_quote_guard_and_no_late_chase(
    tmp_path, monkeypatch, case, legs, outcome
):
    mt, rows, _, _ = tape(tmp_path, monkeypatch, case=case)
    assert len([q for q in mt.requests if "position" not in q]) == legs
    assert [r["outcome"] for r in rows if r["event"] == "label_decision"] == [outcome]
    if case == "interrupt":
        assert not mt.positions and any(
            r.get("reason") == "opposite_m1_hull" for r in rows
        )
    if case == "wait":
        assert any(r.get("disposition") == "wait" for r in rows)


@pytest.mark.parametrize(
    "case,armed", [("missing", True), ("entry", False), ("exit", True)]
)
def test_missing_context_observer_and_cross_parent_live_exit(
    tmp_path, monkeypatch, case, armed
):
    mt, rows, _, _ = tape(tmp_path, monkeypatch, case=case, armed=armed)
    if case == "exit":
        assert len(mt.requests) == 8 and not mt.positions
        assert (
            len(
                [
                    r
                    for r in rows
                    if r["event"] == "exit_request"
                    and r["reason"] == "opposite_m1_hull"
                ]
            )
            == 4
        )
    else:
        assert not mt.requests
        assert any(
            r.get("outcome")
            == ("m1_parent_or_history_unavailable" if armed else "shadow_plan_only")
            for r in rows
        )


@pytest.mark.parametrize("side", [-1, 1])
def test_context_no_hindsight_mixed_parents_and_first_cross_policy(side):
    m = Stream(1, P, 0.01, tick_size=0.001, price_only=True)
    path = bars(side, 1, end=S)
    raw = [m.h.update(b) for b in path][-1]
    assert m.h.structure.view.bias == 0 and m.h.structure.view.closed_s is None
    for close in (S + 1, S - 5 * 60):
        assert candidate(raw, path[-1], Structure(close, side), 5, m, S + 0.2) is None
    mixed = replace(raw, cross_high=-1, cross_low=1)
    b, s = [
        candidate(mixed, path[-1], Structure(T, sign), tf, m, S + 0.1)
        for tf, sign in [(30, 1), (15, -1)]
    ]
    assert choose([b, s]) == (None, "conflicting_m1_parents")
    gate = FirstCrossGate()
    first = replace(b, structure=Structure(T, 1, last_mss_s=T))
    assert gate.apply(first, observe=True).entry_side == 0
    assert gate.apply(replace(first, close_s=S + 60), observe=True).entry_side == 1
    resumed = replace(
        s,
        structure=Structure(
            T, -1, -2, established_bias=-1, failed_counter_mss_s=T - 1800
        ),
    )
    assert FirstCrossGate().apply(resumed, observe=True).waived_first_cross
    assert (
        not FirstCrossGate().apply(replace(resumed, close_s=T), observe=True).entry_side
    )
    assert choose([b, replace(b, tf=45)])[0].tf == 45
    assert candidate(raw, path[-1], Structure(T, 0), 15, m, S + 0.1) is None
    with pytest.raises(ValueError):
        candidate(
            replace(raw, trigger=LiveTick(S * 1000, 2, 3)),
            path[-1],
            Structure(T, 1),
            15,
            m,
            S + 0.1,
        )


def test_symbol_wide_live_exit_scoped_to_proven_minute_tickets_and_recoverable(
    hull_desk,
):
    from copy import copy
    from types import SimpleNamespace

    mt, d, _ = hull_desk("medium")
    stamp = T * 1000 + 150

    def pos(ticket, tf, side, source, *, sym=NAME, magic=None, ms=None):
        p = SimpleNamespace(
            ticket=ticket,
            identifier=ticket,
            time_msc=ms or stamp - 100,
            magic=d.h.magic if magic is None else magic,
            symbol=sym,
            type=d.typ(side),
            sl=1999 if side == 1 else 2002,
            tp=2002 if side == 1 else 1999,
            volume=0.1,
            price_open=2000,
            profit=0,
        )
        mt.positions.append(p)
        if tf is not None:
            assert d.owners.bind(p, tf, minute_source=source)
        return p

    pos(11, 5, 1, "low")
    pos(12, 45, 1, "low")
    pos(13, 15, -1, "high")
    pos(14, 5, 1, None)
    pos(15, None, 1, None)
    pos(16, 30, 1, "low", sym="OTHER")
    pos(17, 30, 1, "low", magic=7)
    pos(18, 30, 1, "low", ms=stamp + 1)
    ms = mt.q.time_msc = stamp + 10
    # High-source true SELL from a bearish parent must also protect Low-source BUYs
    # owned by entirely different parents. First inventory RPC fails: fence survives.
    get = mt.positions_get
    mt.positions_get = lambda **kw: None
    assert (
        d.minute_opposite(
            NAME, {"high": -1}, stamp, live=True, active_sources={"low", "high"}
        )
        == 0
    )
    mt.positions_get = get
    _, d2, _ = hull_desk("medium", state=json.loads(d.path.read_text()), mt=mt)
    d2.supervise()
    ids = {p.ticket for p in mt.positions}
    assert {r["position"] for r in mt.requests} == {11, 12} and ids == set(
        range(13, 19)
    )
    assert not d2.exits and mt.q.time_msc == ms
    d2.supervise()
    assert len(mt.requests) == 2
    # A recycled fingerprint does not confer an M1 origin.
    recycled = copy(mt.positions[0])
    recycled.price_open += 1
    assert d2.owners.minute_source(recycled) is None


def test_one_symbol_reservation_cannot_fall_back_after_restart(hull_desk):
    from hull_mt import sig

    mt, d, _ = hull_desk("medium")
    d.h = d.h.model_copy(
        update={"execution_mode": M1_MODE, "eligible_timeframes": ("M5", "M30")}
    )
    raw = replace(
        sig(),
        tf=30,
        trigger_tf=1,
        anchor_s=T - 60,
        detected_s=T + 0.1,
        stop_tick_size=0.001,
    )
    d.verify_signal = lambda *_: False
    assert (
        d.batch(NAME, raw) == "next_bar_quote_or_context_unverified" and not mt.requests
    )
    _, again, _ = hull_desk("medium", state=json.loads(d.path.read_text()), mt=mt)
    again.h = d.h
    again.verify_signal = lambda *_: True
    assert (
        again.batch(NAME, replace(raw, tf=5)) == "already_consumed" and not mt.requests
    )
    assert again.batch(NAME, replace(raw, trigger_tf=None)) == "execution_mode_mismatch"


def test_journal_uses_m1_clock_and_parent_ownership_on_actual_deals_only(
    tmp_path, monkeypatch
):
    from contextlib import closing

    monkeypatch.syspath_prepend(str(ROOT / "HullJournal"))
    monkeypatch.syspath_prepend(str(ROOT / "HullJournal/tests"))
    from fixtures import FakeMT, deal
    from ledger import db_open, get, iso, make_views, sync

    mt, _, _, _ = tape(tmp_path, monkeypatch, side=-1, frames=(5, 30))
    broker = FakeMT([], S + 180)
    broker.login, broker.host = 1, "t"
    with closing(db_open(tmp_path / "journal.sqlite3")) as db:
        sync(db, broker, broker.at, repo=tmp_path)
        assert make_views(db)[0]["tickets"] == []
        for p in mt.positions:
            broker.ds.extend(
                [
                    deal(
                        p.ticket,
                        p.identifier,
                        S,
                        1,
                        0,
                        0.1,
                        p.price_open,
                        comment="rewritten",
                        ms=125,
                        commission=-0.2,
                    ),
                    deal(
                        p.ticket + 100,
                        p.identifier,
                        S + 10,
                        0,
                        1,
                        0.1,
                        p.tp,
                        profit=2.8,
                        fee=-0.1,
                        magic=0,
                    ),
                ]
            )
        sync(db, broker, broker.at + 1, repo=tmp_path, reconcile=True)
        data = make_views(db)[0]
        assert (
            len(data["tickets"]) == 4
            and len(data["setups"]) == 1
            and get(db, "log_cursor")["bad"] == 0
        )
        for row in [*data["setups"], *data["tickets"]]:
            assert (
                row["timeframe"],
                row["entry_timing"],
                row["initial_stop_policy"],
                row["setup_type"],
            ) == ("M30", "confirmed_m1_close", M1_STOP, "m1_parent_structure_cross")
            assert row["signal_bar_open_utc"] == iso((S - 60) * 1000) and row[
                "signal_close_utc"
            ] == iso(S * 1000)
        assert (
            data["setups"][0]["net_profit"] == "10.00"
            and not data["setups"][0]["send_bid"]
        )
        assert all(r["send_bid"] and r["frozen_cross_sl"] for r in data["tickets"])


def test_explicit_cli_override_does_not_authorize_m1_lane_or_arm(tmp_path, monkeypatch):
    import yaml
    from pydantic import ValidationError

    from bot.cli import main

    old = load_yaml(ROOT / "config.yaml")
    old["hull"].pop("execution_mode")  # legacy full YAML with approved parents
    old["hull"]["eligible_timeframes"] = ["M15", "M30", "M45"]
    (tmp_path / "old.yaml").write_text(yaml.safe_dump(old))
    calls = []
    monkeypatch.setattr("bot.hull.terminal.run", lambda *a, **kw: calls.append((a, kw)))
    main(
        [
            "hull-preflight",
            "--config",
            str(tmp_path / "old.yaml"),
            "--m1-confirm",
            "--timeframe",
            "M30",
        ]
    )
    assert (
        calls[0][0][1].execution_mode == M1_MODE
        and calls[0][0][3] == (30,)
        and not calls[0][1]["armed"]
    )
    cfg = old["hull"]
    assert HullConfig.model_validate(cfg).execution_mode == "parent_close"
    with pytest.raises(ValidationError):
        HullConfig.model_validate(
            cfg | {"eligible_timeframes": ["M1"], "execution_mode": M1_MODE}
        )


@pytest.mark.parametrize("side", [-1, 1])
def test_forming_m1_cross_that_uncrosses_never_generates_an_entry(
    tmp_path, monkeypatch, side
):
    mt, rows, _, _ = tape(tmp_path, monkeypatch, side=side, case="flicker")
    assert any(
        r["event"] == "m1_raw_exit_signal"
        and r["phase"] == "live"
        and side in r["crosses"].values()
        for r in rows
    )
    assert not mt.requests and not any(
        r["event"] in ("batch_preview", "demo_result") for r in rows
    )


def test_preflight_fails_closed_on_missing_parent_and_never_starts_scanner(
    tmp_path, monkeypatch
):
    from bot.broker import DemoSafetyError

    mt, summary, _, _ = tape(tmp_path, monkeypatch, preflight=True, armed=False)
    assert (
        summary["m1_history_ready"]
        and summary["history_check"][NAME]["M1 trigger"]["closed_bars_loaded"] > 20
    )
    assert not mt.requests and not (tmp_path / "reports").exists()
    with pytest.raises(DemoSafetyError, match="history unavailable"):
        tape(tmp_path, monkeypatch, preflight=True, armed=False, case="missing")


def test_open_minute_tickets_cannot_be_silently_switched_to_legacy_manager(
    tmp_path, monkeypatch
):
    from bot.broker import DemoSafetyError

    mt, _, _, _ = tape(tmp_path, monkeypatch)
    n = len(mt.requests)
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["run"]["mode"] = "demo"
    h = HullConfig.model_validate(cfg["hull"])
    with pytest.raises(DemoSafetyError, match="Open M1-origin ticket"):
        run(cfg, h, selection(h, rate="medium"), armed=True, iterations=1)
    assert len(mt.requests) == n and len(mt.positions) == 4
