"""Independent books, causal M45 data and write-ahead exits. Fake broker only."""

import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_mt import T, sig

from bot.hull.bar import Bar
from bot.hull.config import HullConfig, Indicator
from bot.hull.entry_clock import WAITING
from bot.hull.feed import Stream, rates
from bot.hull.m45 import aggregate
from bot.hull.signal import LiveTick
from bot.hull.structure import Structure
from bot.hull.terminal import dispatch

NAME = "XAUUSDm"
OK = {"data_ok": True, "history_ok": True, "rewarmed": False}


def setup(hull_desk, **opts):
    mt, d, log = hull_desk("medium", **opts)
    d.h = HullConfig.model_validate(
        d.h.model_dump() | {"eligible_timeframes": ["M15", "M30", "M45"]}
    )
    return mt, d, log


def reading(tf, side=1, end=T):
    return replace(
        sig(1999 if side == 1 else 2001, side),
        tf=tf,
        close_s=end,
        structure=Structure(end, side),
    )


def mss(tf, side=-1, end=T):
    return replace(
        reading(tf, side, end),
        structure=Structure(end, side, 2 * side),
        label=0,
        side=0,
        raw_sl=None,
    )


@pytest.mark.parametrize("exit_tf", [15, 30, 45])
@pytest.mark.parametrize("direction", [-1, 1])
def test_real_fill_ids_three_books_one_symbol_restart_and_only_own_MSS(
    hull_desk, exit_tf, direction
):
    mt, d, log = setup(hull_desk)
    mt.q.time_msc = (T - 1) * 1000 + 100
    dispatch(
        d,
        NAME,
        [reading(15, 1, T - 1), reading(30, -1, T - 1), reading(45, 1, T - 1)],
        **OK,
    )
    assert len(mt.positions) == 12
    old = {p.ticket: (d.owners.lane(p), p.sl, p.tp) for p in mt.positions}
    assert {tf for tf, _, _ in old.values()} == {15, 30, 45}
    restored = json.loads(d.path.read_text())
    _, d, log = setup(hull_desk, mt=mt, state=restored)
    mt.requests.clear()
    mt.q.time_msc = T * 1000 + 100
    shift = mss(exit_tf, direction)
    dispatch(
        d,
        NAME,
        [],
        shifts=[shift, shift],
        data_ok=False,
        history_ok=False,
        rewarmed=True,
    )
    assert {r["position"] for r in mt.requests} == {
        t for t, (f, _, _) in old.items() if f == exit_tf
    }
    assert len(mt.requests) == 4 and all(
        (p.sl, p.tp) == old[p.ticket][1:] for p in mt.positions
    )
    assert [x["tf"] for e, x in log if e == "lane_mss_exit"] == [exit_tf]
    assert not any(e == "batch_preview" for e, _ in log)
    n = len(mt.requests)
    d.supervise()
    assert len(mt.requests) == n
    # Opening identifiers also scope post-trigger SL/manual deals (magic 0).
    other = next(p for p in mt.positions if d.owners.lane(p) != exit_tf)
    mt.deals = [
        NS(
            symbol=NAME,
            entry=1,
            type=1,
            magic=0,
            position_id=other.identifier,
            time_msc=T * 1000 + 150,
        )
    ]
    d._check_flat_at_trigger(NAME, reading(exit_tf), T + 0.3)


def test_MSS_not_from_wick_init_bos_old_context_wrong_lane_or_other_symbol(hull_desk):
    mt, d, _log = setup(hull_desk)
    shift = mss(15)
    for v in [
        replace(shift, trigger=LiveTick.read(mt.q)),
        replace(shift, structure=Structure(T - 900, -1, -2)),
        replace(shift, structure=Structure(T, 1, 1)),
        replace(shift, structure=Structure(T, -1, -3)),
        replace(shift, structure=Structure(T, 1, -2)),
        replace(shift, tf=1),
        mss(15, end=T + 900),
    ]:
        assert d.close_mss(NAME, v) == 0
    assert not d.mss_book and not mt.requests
    assert d.close_mss("OTHER", shift) == 0 and not d.mss_book


def test_lost_inventory_restart_stale_quotes_uncertain_ack_new_and_untagged_never_swept(
    hull_desk,
):
    mt, d, _ = setup(hull_desk)
    mt.q.time_msc = (T - 1) * 1000 + 100
    assert d.batch(NAME, reading(15, 1, T - 1)) == "demo_batch_sent"
    assert d.batch(NAME, reading(30, -1, T - 1)) == "demo_batch_sent"
    same = [p.ticket for p in mt.positions if d.owners.lane(p) == 15]
    template = mt.positions[0]
    foreign = NS(**(vars(template) | {"magic": 0, "ticket": 70, "identifier": 70}))
    legacy = NS(
        **(vars(template) | {"ticket": 71, "identifier": 71, "comment": f"h15_{T}_1"})
    )
    newer = NS(
        **(vars(template) | {"ticket": 72, "identifier": 72, "time_msc": T * 1000 + 1})
    )
    other = NS(**(vars(template) | {"ticket": 73, "identifier": 73, "symbol": "OTHER"}))
    mt.positions.extend([foreign, legacy, newer, other])
    assert d.owners.bind(newer, 15)
    original = mt.positions_get
    mt.positions_get = lambda *a, **k: None
    d.close_mss(NAME, mss(15))
    assert d.mss_book[NAME]["15"] == T
    _, d, _log = setup(hull_desk, mt=mt, state=json.loads(d.path.read_text()))
    mt.positions_get = original
    mt.requests.clear()
    mt.q.time_msc = (T - 15) * 1000
    d.supervise()
    assert not mt.requests and set(d.exits) == set(map(str, same))
    mt.q.time_msc = T * 1000 + 200
    mt.bad = True
    d.supervise()
    d.supervise()
    assert [r["position"] for r in mt.requests] == same and all(
        x["status"] == "verify_or_review" for x in d.exits.values()
    )
    assert all(p in mt.positions for p in [foreign, legacy, newer, other])


def test_lane_windows_and_data_availability_are_independent(hull_desk):
    mt, d, log = setup(hull_desk, armed=False)
    q = mt.q
    mt.q = None
    for tf in (15, 30, 45):
        assert d.batch(NAME, reading(tf)) == WAITING
    d.close_mss(NAME, mss(15))
    assert set(d.waiting) == {(NAME, 30), (NAME, 45)}
    mt.q = q
    dispatch(d, NAME, [], data_ok={30: True, 45: False}, history_ok=True, rewarmed={})
    assert {(x["tf"], x["outcome"]) for e, x in log if e == "label_decision"} == {
        (15, "confirmed_mss_exit"),
        (30, "shadow_plan_only"),
        (45, "data_or_deal_history_unavailable"),
    }
    assert not mt.requests


def base(t, v):
    return {"time": t, "open": v, "high": v + 3, "low": v - 1, "close": v + 1}


def test_m45_exact_daily_anchor_partial_missing_day_and_stale_tail():
    # Broker day deliberately NOT aligned to 2700 UTC seconds. Anchors can move at DST.
    D = 1700006400 + 3600
    assert D % 2700
    src = [base(D + i * 900, 100 + i) for i in range(6)]
    days = [{"time": D}]
    rows = aggregate(src, days)
    assert [(r["time"], r["open"], r["high"], r["low"], r["close"]) for r in rows] == [
        (D, 100, 105, 99, 103),
        (D + 2700, 103, 108, 102, 106),
    ]
    assert aggregate(src[1:3], days) == [] and aggregate(src[:2], days) == []
    assert aggregate(src[:1] + src[2:], days) == rows[1:] and aggregate(src, []) == []
    assert aggregate(src[:3] + src[2:3], days) == []
    native = NS(TIMEFRAME_M15=15, TIMEFRAME_D1=16408)

    def copy(_, period, pos, n):
        assert period == 15
        return src[:3] if pos == 1 else src[2:5]

    native.copy_rates_from_pos = copy
    native.copy_rates_range = lambda sym, period, start, end: (
        days if period == 16408 else None
    )
    assert rates(native, NAME, 45, 1, 1) == rows[:1]
    assert rates(native, NAME, 45, 0, 1)[0]["close"] == 105
    lane = Stream(45, Indicator(), 0.01)
    lane.h.update(Bar(D, 100, 105, 99, 103))
    assert lane.current_bar(native, NAME, LiveTick((D + 3600) * 1000 + 10, 105, 106))
    assert (
        lane.current_bar(native, NAME, LiveTick((D + 4500) * 1000 + 10, 105, 106))
        is None
    )


def test_terminal_detects_standalone_native_shift_without_label_and_no_attach_exit(
    hull_desk, tmp_path, monkeypatch
):
    """Exercise the actual poll/refresh/supervise path with three populated books."""
    import hashlib
    import random
    import sys

    from hull_mt import ROOT, scanner_clock

    from bot.config.loader import load_yaml
    from bot.hull.config import selection
    from bot.hull.signal import Hull
    from bot.hull.terminal import run

    mt, d, _ = setup(hull_desk)
    D = 1700006400 + 3600
    rng = random.Random(15)
    price = 1400.0
    bars = []
    engine = Hull(15, Indicator(), 0.01)
    idx = None
    for i in range(650):
        o = price
        price += rng.choice([-5, -3, -1, 0, 1, 4, 7])
        b = Bar(
            D + i * 900,
            o,
            max(o, price) + rng.randrange(4),
            min(o, price) - rng.randrange(4),
            price,
        )
        bars.append({"time": b.t, "open": b.o, "high": b.h, "low": b.l, "close": b.c})
        r = engine.update(b)
        if (
            idx is None
            and i > 140
            and r.confirmed_mss
            and not r.label
            and (i + 1) % 2
            and (i + 1) % 3
        ):
            idx = i
    assert idx is not None
    stamp = D + (idx + 1) * 900
    wall = [stamp - 0.2]
    for tf in (15, 30, 45):
        p = NS(
            symbol=NAME,
            magic=d.h.magic,
            ticket=tf,
            identifier=tf,
            type=0,
            price_open=1400.0,
            time_msc=(stamp - 3600) * 1000,
            sl=1200.0,
            tp=2000.0,
            volume=0.1,
            profit=0.0,
        )
        mt.positions.append(p)
        assert d.owners.bind(p, tf)
    d.state["manual_hold"] = {
        "symbol": NAME,
        "test": "prevents new orders, never protective exits",
    }
    home = tmp_path / "mock-home"
    private = (
        home
        / ".moneyprinterbot"
        / "hull"
        / hashlib.sha256(repr((1, "t")).encode()).hexdigest()[:16]
    )
    private.mkdir(parents=True)
    (private / "state.json").write_text(json.dumps(d.state))

    def rows(_name, period, pos, n):
        cur = bars[: int((wall[0] - D) // 900) + 1]
        if period == 30:
            cur = [
                {
                    "time": s[0]["time"],
                    "open": s[0]["open"],
                    "high": max(b["high"] for b in s),
                    "low": min(b["low"] for b in s),
                    "close": s[-1]["close"],
                }
                for j in range(0, len(cur), 2)
                if (s := cur[j : j + 2])
            ]
        else:
            assert period == mt.TIMEFRAME_M15
        return (cur[:-1] if pos else cur)[-n:]

    mt.copy_rates_from_pos = rows
    mt.copy_rates_range = lambda _, period, a, b: [
        {"time": D + k * 86400}
        for k in range(8)
        if a.timestamp() <= D + k * 86400 <= b.timestamp()
    ]

    def tick(_):
        bid = bars[int((wall[0] - D) // 900)]["close"]
        return NS(time_msc=int(wall[0] * 1000), bid=bid, ask=bid + 0.01)

    mt.symbol_info_tick = tick
    mt.initialize = lambda: True
    mt.shutdown = lambda: None
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(tmp_path)
    scanner_clock(
        monkeypatch, lambda: wall[0], lambda _: wall.__setitem__(0, stamp + 0.125)
    )
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["hull"].update(
        execution_mode="parent_close", eligible_timeframes=["M15", "M30", "M45"]
    )
    cfg["run"]["mode"] = "demo"
    h = HullConfig.model_validate(cfg["hull"])
    run(cfg, h, selection(h), armed=True, iterations=3)
    assert [x["position"] for x in mt.requests] == [15] and [
        p.ticket for p in mt.positions
    ] == [30, 45]
    events = [
        json.loads(line)
        for line in next(tmp_path.glob("reports/hull_runtime/*/demo.jsonl"))
        .read_text()
        .splitlines()
    ]
    assert [
        (e["tf"], e["close_s"]) for e in events if e["event"] == "lane_mss_exit"
    ] == [(15, stamp)]
    assert not any(
        e["event"] == "closed_label" and e["tf"] == 15 and e["close_s"] == stamp
        for e in events
    )


def test_aggregate_caps_are_not_multiplied_by_lane(hull_desk):
    mt, d, _ = setup(hull_desk)
    d.lim = d.lim.model_copy(update={"max_positions": 4})
    assert d.batch(NAME, reading(15)) == "demo_batch_sent"
    assert d.batch(NAME, reading(30, -1)) == "portfolio_position_limit"
    assert len(mt.positions) == 4 and {d.owners.lane(p) for p in mt.positions} == {15}


def test_batch_boundary_peek_preserves_the_lane_entry_cursor_and_live_state():
    import random

    from bot.hull.signal import Hull

    tf = 15
    D = T - (T % 900) - 1000 * 900
    cfg = Indicator()
    lane = Stream(tf, cfg, 0.01)
    ref = Hull(tf, cfg, 0.01)
    rng = random.Random(17)
    rows = []
    c = 1200.0
    for i in range(800):
        o = c
        c += rng.choice([-7, -4, -1, 1, 3, 6])
        b = Bar(D + i * 900, o, max(c, o) + 1, min(c, o) - 1, c)
        v = ref.update(b)
        rows.append({"time": b.t, "open": b.o, "high": b.h, "low": b.l, "close": b.c})
        if i > 100 and v.confirmed_mss:
            break
        lane.h.update(b)
    assert v.confirmed_mss
    mt = NS(TIMEFRAME_M15=15, copy_rates_from_pos=lambda *args: rows[-4:])
    now = v.close_s + 0.1
    before = (lane.h.n, lane.h.structure.view, dict(lane.h.gate.record))
    assert lane.boundary_shifts(mt, NAME, now) == [v.structure]
    assert lane.boundary_shifts(mt, NAME, now) == []
    assert before == (lane.h.n, lane.h.structure.view, lane.h.gate.record)
    lane.baseline_due = False
    event, status = lane.refresh(mt, NAME, now)
    assert status == "updated" and event == v and lane.shifts == [event]


def test_partial_mss_ack_only_reconciled_remaining_volume_on_new_quote(hull_desk):
    mt, d, _ = setup(hull_desk)
    mt.q.time_msc = (T - 1) * 1000 + 100
    assert d.batch(NAME, reading(45, 1, T - 1)) == "demo_batch_sent"
    p = mt.positions[0]
    mt.positions[:] = [p]
    mt.requests.clear()
    mt.q.time_msc = T * 1000 + 100
    send = mt.order_send

    def partial(req):
        mt.requests.append(req.copy())
        p.volume = 0.04
        return NS(retcode=10010)

    mt.order_send = partial
    d.close_mss(NAME, mss(45))
    assert d.exits[str(p.ticket)]["status"] == "verify_partial"
    mt.order_send = send
    d.supervise()
    assert len(mt.requests) == 1
    mt.q.time_msc += 1
    d.supervise()
    assert (
        [r["volume"] for r in mt.requests] == [0.1, 0.04]
        and not mt.positions
        and not d.exits
    )
