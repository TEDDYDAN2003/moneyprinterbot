"""Real Hull/structure/scanner/desk, deterministic native Bid candles and in-process fake broker."""

import json
import sys
from dataclasses import replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from random import Random
from types import SimpleNamespace as NS

import pytest
from hull_mt import MT, ROOT, T, scanner_clock

from bot.broker import DemoSafetyError
from bot.config.loader import load_yaml
from bot.hull.bar import Bar
from bot.hull.config import INTRA_ENTRY, INTRA_STOP, HullConfig, selection
from bot.hull.curves import LiveTick
from bot.hull.intrabar_guard import relation
from bot.hull.intrabar_signal import candidate
from bot.hull.orders import cross_candle_stop
from bot.hull.signal import FirstCrossGate, Hull
from bot.hull.terminal import run

NAME = "XAUUSDm"
OFF, ON = 2005.9, 2010.0


def prefix(tf, side=1, scale=1):
    # Same seeded price scale for both mirrors. Confirm the range swing with
    # three bars on the right, using the production Dynamic 16/3 parameters.
    rng = Random(10)
    px = lambda n: round(2006.2 + side * (n - 2006.2) * scale, 4)
    mid, out = 2000, []
    for i in range(65):
        op = mid
        mid = round(op + rng.choice([-1.6, -1.0, -0.5, 0, 0.5, 1, 1.6]), 2)
        hi = max(op, mid) + rng.randrange(1, 5) * 0.1
        lo = min(op, mid) - rng.randrange(1, 5) * 0.1
        high, low = sorted((px(hi), px(lo)), reverse=True)
        out.append(Bar(T - (65 - i) * tf * 60, px(op), high, low, px(mid)))
    return out


class Tape:
    def __init__(self, root, mp, timeline, *, side=1, parents=(5,), child=3):
        self.root, self.mp, self.side = root, mp, side
        self.cfg = load_yaml(ROOT / "config.yaml")
        self.cfg["run"]["mode"] = "demo"
        self.cfg["hull"].update(
            eligible_timeframes=[f"M{p}" for p in parents],
            pairs={5: child, 10: 5, 15: 10},
        )
        self.mt = MT()
        self.mt.TIMEFRAME_M4 = 4
        self.mt.i.trade_tick_size = 0.025
        self.timeline = list(timeline)
        self.i = 0
        self.bad = lambda tf, pos: False
        self.hidden = []  # additional native prints, not sampled by the observer
        self.px = lambda v: round(2006.2 + side * (v - 2006.2), 4)
        children = {self.cfg["hull"]["pairs"][p] for p in parents}
        self.paths = {
            f: prefix(f, side, 1 if f in children else 1.5)
            for f in children | set(parents)
        }
        self.mt.copy_rates_from_pos = self.rates
        self.mt.symbol_info_tick = self.tick
        self.mt.initialize = lambda: True
        self.mt.shutdown = lambda: None
        mp.chdir(root)
        mp.setenv("HOME", str(root))
        mp.setenv("USERPROFILE", str(root))
        mp.setitem(sys.modules, "MetaTrader5", self.mt)
        scanner_clock(mp, self.clock, lambda _: setattr(self, "i", self.i + 1))

    def clock(self):
        return (T * 1000 + self.timeline[self.i][0]) / 1000

    def tick(self, _):
        off, v = self.timeline[self.i]
        self.mt.q = NS(
            bid=self.px(v), ask=round(self.px(v) + 0.05, 4), time_msc=T * 1000 + off
        )
        return self.mt.q

    def rates(self, _, tf, pos, n):
        if self.bad(tf, pos):
            return None
        rows = list(self.paths[tf])
        now = self.timeline[self.i][0]
        points = sorted(
            [*self.timeline[: self.i + 1], *self.hidden], key=lambda x: x[0]
        )
        start = 0
        while start <= now:
            op = rows[-1].c
            vv = [
                self.px(v)
                for off, v in points
                if start <= off < start + tf * 60000 and off <= now
            ]
            bar = Bar(
                T + start // 1000,
                op,
                max([op, *vv]),
                min([op, *vv]),
                vv[-1] if vv else op,
            )
            if now < start + tf * 60000:
                if not pos:
                    rows = [bar]
                break
            rows.append(bar)
            start += tf * 60000
        return [
            {"time": b.t, "open": b.o, "high": b.h, "low": b.l, "close": b.c}
            for b in rows[-n:]
        ]

    def run(self, *, count=None, start=None, armed=True, preflight=False):
        if start is not None:
            self.i = start
        h = HullConfig.model_validate(self.cfg["hull"])
        return run(
            self.cfg,
            h,
            selection(h, rate="medium"),
            armed=armed,
            preflight=preflight,
            iterations=count or len(self.timeline) - self.i,
        )

    def events(self, kind, *, shadow=False):
        log = next(
            self.root.glob(
                "reports/hull_runtime/*/" + ("shadow.jsonl" if shadow else "demo.jsonl")
            )
        )
        return [
            r
            for line in log.read_text().splitlines()
            if (r := json.loads(line))["event"] == kind
        ]

    def state(self):
        return json.loads(
            next(self.root.glob(".moneyprinterbot/hull/*/state.json")).read_text()
        )


@pytest.mark.parametrize("child", [3, 4])
@pytest.mark.parametrize("side", [-1, 1])
def test_first_real_intrabar_cross_and_same_second_recross_after_flat(
    tmp_path, monkeypatch, side, child
):
    tape = Tape(
        tmp_path,
        monkeypatch,
        [(100, OFF), (200, ON), (300, ON), (400, OFF), (500, ON), (600, ON)],
        side=side,
        child=child,
    )
    tape.run()
    res = tape.events("demo_result")
    closes = tape.events("exit_request")
    assert len(res) == 8 and len(closes) == 4 and len(tape.mt.positions) == 4
    assert [r["trigger_msc"] - T * 1000 for r in res] == [200] * 4 + [500] * 4
    assert len({r["request"]["comment"] for r in res}) == 8
    assert [r["ticket"] for r in closes] == [1, 2, 3, 4]
    assert all(
        r["reason"] == "origin_cross_uncrossed" and r["trigger_tf"] == child
        for r in closes
    )
    assert not tape.events("closed_label") and not tape.events("pair_arbitration")
    assert [r["outcome"] for r in tape.events("label_decision")] == [
        "demo_batch_sent"
    ] * 2
    for r in res:
        assert (
            r["entry_policy"] == INTRA_ENTRY and r["initial_stop_policy"] == INTRA_STOP
        )
        assert (
            r["parent_tf"],
            r["trigger_tf"],
            r["parent_structure_bias"],
            r["trigger_structure_bias"],
        ) == (5, child, side, side)
        assert r["swing_price"] == tape.px(1999.6) and r["swing_kind"] == (
            "HL" if side == 1 else "LH"
        )
        assert r["swing_price"] not in (tape.px(OFF), tape.px(ON))
        assert (
            r["swing_bar_s"] < r["swing_known_s"] <= r["trigger_structure_close_s"] == T
        )
        assert r["signal_close_utc"] is None and r["origin_bar_end_s"] == T + child * 60
        assert (
            r["trigger_to_submit_ms"] == 0
            and "close_to_submit_ms" not in r
            and "cross_candle_wick" not in r
        )
        assert r["frozen_swing_sl"] == cross_candle_stop(
            r["swing_price"], side, 0.1 * r["cross_atr"], 0.025
        )
        tick = Decimal("0.025")
        rounded_cross = float(
            tick
            * (Decimal(str(r["hull_cross_price"])) / tick).to_integral_value(
                rounding=ROUND_FLOOR if side == 1 else ROUND_CEILING
            )
        )
        expected_raw = (
            max(r["frozen_swing_sl"], rounded_cross)
            if side == 1
            else min(r["frozen_swing_sl"], rounded_cross)
        )
        assert r["cross_region_raw_stop"] == rounded_cross
        assert r["raw_chart_sl"] == expected_raw
        assert r["cross_region_action"] == (
            "cross_region_tightened"
            if expected_raw == rounded_cross
            and rounded_cross != r["frozen_swing_sl"]
            else "existing_base_stop_retained"
        )
        assert r["request"]["sl"] == pytest.approx(
            expected_raw + (0.05 if side == -1 else 0)
        )
    state = tape.state()
    records = list(state["hull_lane_positions_v1"].values())
    assert [
        r["intrabar_origin"]["status"]
        for r in sorted(records, key=lambda v: v["ticket"])
    ] == ["invalid"] * 4 + ["watch"] * 4
    assert NAME not in state.get("pair_consumed", {}) and not state.get("manual_hold")


@pytest.mark.parametrize("last,why", [(ON, None), (OFF, "origin_cross_uncrossed")])
@pytest.mark.parametrize("restart", [False, True])
def test_origin_close_only_catchup_and_successor_no_label(
    tmp_path, monkeypatch, last, why, restart
):
    tape = Tape(
        tmp_path, monkeypatch, [(100, OFF), (200, ON), (180100, ON), (180200, ON)]
    )
    tape.hidden = [(179990, last)]
    if restart:
        tape.run(count=2)
        tape.run(start=2)
    else:
        tape.run()
    assert len(tape.events("demo_result")) == 4
    assert len(tape.mt.positions) == (0 if why else 4)
    assert [r["reason"] for r in tape.events("exit_request")] == (
        [why] * 4 if why else []
    )
    (close,) = tape.events("origin_cross_check")
    assert close["phase"] == "final_close" and close["observed_msc"] == (T + 180) * 1000
    assert not tape.events("closed_label")


def test_confirmed_cross_keeps_independent_child_opposite_exit(tmp_path, monkeypatch):
    tape = Tape(
        tmp_path, monkeypatch, [(100, OFF), (200, ON), (180100, ON), (180200, 2000.0)]
    )
    tape.run()
    assert len(tape.events("demo_result")) == 4 and not tape.mt.positions
    assert {r["reason"] for r in tape.events("exit_request")} == {
        "opposite_pair_trigger_hull"
    }


@pytest.mark.parametrize(
    "times",
    [
        [(100, ON), (300, ON)],  # startup with held label
        [(100, OFF), (5100, ON), (5200, ON)],  # no edge across an observation gap
        [(100, OFF), (100, ON), (200, ON)],  # ambiguous same-ms prints
        [(200, OFF), (100, ON), (300, ON)],  # backwards timestamp
    ],
)
def test_baselines_not_entries(tmp_path, monkeypatch, times):
    tape = Tape(tmp_path, monkeypatch, times)
    tape.run()
    assert not tape.mt.requests and not tape.events("demo_result")


def test_restart_held_label_no_duplicate_first_watch_can_verify_uncross(
    tmp_path, monkeypatch
):
    tape = Tape(
        tmp_path, monkeypatch, [(100, OFF), (200, ON), (300, ON), (400, OFF), (500, ON)]
    )
    tape.run(count=2)
    tape.run(start=2, count=1)
    assert len(tape.mt.requests) == 4
    tape.run(start=3)
    assert (
        len(tape.events("demo_result")) == 8 and len(tape.events("exit_request")) == 4
    )
    assert len(tape.mt.positions) == 4


def test_cannot_drop_intrabar_manager_while_open(tmp_path, monkeypatch):
    tape = Tape(tmp_path, monkeypatch, [(100, OFF), (200, ON), (300, ON)])
    tape.run(count=2)
    tape.cfg["hull"]["entry_timing"] = "confirmed_cross_next_bar"
    with pytest.raises(DemoSafetyError, match="while open"):
        tape.run(start=2)
    assert len(tape.mt.requests) == 4


def test_fade_between_legs_closes_only_real_fill_no_topup(tmp_path, monkeypatch):
    tape = Tape(tmp_path, monkeypatch, [(100, OFF), (200, ON), (240, OFF), (400, OFF)])
    send = tape.mt.order_send

    def interrupt(req):
        result = send(req)
        if not req.get("position") and tape.i == 1:
            tape.i = 2
        return result

    tape.mt.order_send = interrupt
    tape.run(count=2)
    assert not tape.mt.positions and len(tape.mt.requests) == 2
    assert (
        tape.events("label_decision")[0]["outcome"]
        == "protected_partial_no_topup:live_cross_or_context_unverified"
    )
    assert tape.events("exit_request")[0]["reason"] == "origin_cross_uncrossed"


def test_uncertain_exit_ack_blocks_recross_without_blind_close_retries(
    tmp_path, monkeypatch
):
    tape = Tape(
        tmp_path, monkeypatch, [(100, OFF), (200, ON), (300, OFF), (400, ON), (500, ON)]
    )
    send = tape.mt.order_send

    def uncertain(q):
        if q.get("position") == 1:
            tape.mt.requests.append(dict(q))
            return None
        return send(q)

    tape.mt.order_send = uncertain
    tape.run()
    assert len(tape.events("demo_result")) == 4 and [
        p.ticket for p in tape.mt.positions
    ] == [1]
    assert len([x for x in tape.mt.requests if x.get("position") == 1]) == 1
    assert (
        tape.events("label_decision")[-1]["outcome"]
        == "opposite_exit_not_confirmed_flat"
    )
    assert tape.state()["opposite_exits"]["1"]["status"] == "verify_or_review"


@pytest.mark.parametrize("mode", ["missing", "unstable", "stale", "expired", "kill"])
def test_no_closed_fallback_or_parked_order_when_final_quote_unverified(
    tmp_path, monkeypatch, mode
):
    tape = Tape(tmp_path, monkeypatch, [(100, OFF), (200, ON)])
    base = tape.mt.order_check

    def break_check(req):
        checked = base(req)
        if mode == "missing":
            tape.bad = lambda tf, pos: tf == 3 and pos == 0
        elif mode == "unstable":
            original = tape.mt.symbol_info_tick
            i = [0]

            def churn(name):
                q = original(name)
                i[0] += 1
                q.ask += 0.025 * (i[0] % 2)
                return q

            tape.mt.symbol_info_tick = churn
        elif mode == "stale":
            tape.mt.symbol_info_tick = lambda _: None
        elif mode == "expired":
            tape.timeline[tape.i] = (180001, ON)
        else:
            (tmp_path / "KILL").touch()
        return checked

    tape.mt.order_check = break_check
    tape.run()
    assert (
        not tape.mt.requests
        and not tape.mt.positions
        and not tape.state().get("manual_hold")
    )
    assert not any(
        e.get("disposition") == "wait" for e in tape.events("entry_quote_check")
    )


def test_arbitration_one_highest_parent_only(tmp_path, monkeypatch):
    tape = Tape(tmp_path, monkeypatch, [(100, OFF), (200, ON)], parents=(5, 10, 15))
    tape.run()
    (r,) = tape.events("intrabar_arbitration")
    assert r["owner_tf"] == 15 and len(r["candidates"]) == 3
    assert {
        r["request"]["comment"].split("_")[0] for r in tape.events("demo_result")
    } == {"q15x10"}


def test_read_only_default_preflight_and_shadow_have_no_broker_requests(
    tmp_path, monkeypatch
):
    tape = Tape(tmp_path, monkeypatch, [(100, OFF), (200, ON)])
    tape.cfg["run"]["mode"] = "paper"
    summary = tape.run(armed=False, preflight=True)
    assert (
        summary["entry_timing"],
        summary["entry_policy"],
        summary["new_ticket_stop_policy"],
    ) == ("intrabar_cross", INTRA_ENTRY, INTRA_STOP)
    tape.run(armed=False)
    assert not tape.mt.requests and not tape.events("demo_result", shadow=True)
    assert [v["outcome"] for v in tape.events("label_decision", shadow=True)] == [
        "shadow_plan_only"
    ]


def raw_candidate(side=1):
    cfg = HullConfig.model_validate(load_yaml(ROOT / "config.yaml")["hull"]).indicator
    h = Hull(3, cfg, 0.01, tick_size=0.025)
    for b in prefix(3, side):
        h.update(b)
    px = round(2006.2 + side * (ON - 2006.2), 3)
    q = LiveTick(T * 1000 + 150, px, px + 0.05)
    o = h.prev.c
    r = h.preview(Bar(T, o, max(o, px), min(o, px), px), q)
    lane = NS(cfg=cfg, pip=0.01, tick_size=0.025)
    return r, lane


@pytest.mark.parametrize("side", [-1, 1])
def test_swing_confirmed_current_and_strict_no_future_or_wrong_side_fallback(side):
    r, lane = raw_candidate(side)
    c = candidate(r, 5, r.structure, lane, T + 0.15)
    assert c.entry_side == side
    swing = c.swing
    for invalid in [
        None,
        replace(swing, known_s=T + 1),
        replace(swing, price=r.close + side),
    ]:
        bad = replace(r, structure=replace(r.structure, range_swing=invalid))
        assert (
            candidate(bad, 5, c.structure, lane, T + 0.15).entry_reason
            == "trigger_range_swing_unavailable"
        )
    assert not candidate(
        r, 5, replace(r.structure, bias=-side), lane, T + 0.15
    ).entry_side
    equal = replace(r, b=r.a)
    assert relation(equal, r.active_source, side) is False
    assert relation(replace(r, b=None), r.active_source, side) is None
    # Do not use the other A when a close changes the active dynamic source.
    assert (
        relation(
            replace(r, active_source="high" if side == 1 else "low", a=r.b),
            r.active_source,
            side,
        )
        is True
    )


def test_config_and_first_mss_gate_same_candle_epoch_not_bypassed():
    cfg = load_yaml(ROOT / "config.yaml")["hull"]
    assert HullConfig.model_validate(cfg).effective_timing == "intrabar_cross"
    assert (
        HullConfig.model_validate(
            cfg | {"entry_route": "direct_retest_limit"}
        ).effective_timing
        == "confirmed_cross_next_bar"
    )
    assert (
        HullConfig.model_validate(
            {k: v for k, v in cfg.items() if k != "entry_timing"}
        ).effective_timing
        == "confirmed_cross_next_bar"
    )
    with pytest.raises(ValueError, match="retest limits require confirmed"):
        HullConfig.model_validate(
            cfg
            | {"entry_timing": "intrabar_cross", "entry_route": "direct_retest_limit"}
        )
    raw, lane = raw_candidate()
    pv = replace(raw.structure, last_mss_s=T - 300)
    r = candidate(raw, 5, pv, lane, T + 0.15)
    g = FirstCrossGate()
    assert g.apply(r, observe=True).entry_reason == "first_trend_cross_after_mss"
    assert not g.apply(r, observe=False).entry_side
    new = replace(r, trigger=replace(r.trigger, ms=r.signal_msc + 1))
    assert g.apply(new, observe=True).entry_side == 1


def test_unavailable_inventory_latches_origin_before_restart(tmp_path, monkeypatch):
    tape = Tape(tmp_path, monkeypatch, [(100, OFF), (200, ON), (300, OFF), (400, ON)])
    inventory = tape.mt.positions_get
    # Inventory is good at the beginning of the sweep, then unavailable exactly
    # when the zero label arrives. Never forget invalidation if it re-crosses.
    tick = tape.mt.symbol_info_tick
    unavailable = [False]

    def after_read(name):
        q = tick(name)
        if tape.i == 2:
            unavailable[0] = True
        return q

    tape.mt.symbol_info_tick = after_read
    tape.mt.positions_get = lambda *a, **kw: (
        None if unavailable[0] else inventory(*a, **kw)
    )
    tape.run(count=3)
    assert len(tape.mt.positions) == 4 and not tape.events("exit_request")
    assert all(
        v["intrabar_origin"]["status"] == "invalid"
        for v in tape.state()["hull_lane_positions_v1"].values()
    )
    unavailable[0] = False
    tape.run(start=3)
    assert not tape.mt.positions and len(tape.events("demo_result")) == 4
    assert {x["reason"] for x in tape.events("exit_request")} == {
        "origin_cross_uncrossed"
    }


def test_exact_partial_leg_keeps_guard_but_no_ladder_or_topup(tmp_path, monkeypatch):
    tape = Tape(tmp_path, monkeypatch, [(100, OFF), (200, ON), (300, OFF), (400, ON)])
    base = tape.mt.order_send
    deals = []

    def partial(req):
        result = base(req)
        if "position" not in req:
            p = tape.mt.positions[-1]
            p.volume = result.volume = 0.04
            result.retcode = 10010
            deals.append(
                NS(
                    ticket=result.deal,
                    order=result.order,
                    entry=0,
                    type=p.type,
                    position_id=p.identifier,
                    magic=p.magic,
                    symbol=p.symbol,
                    price=p.price_open,
                    volume=p.volume,
                    time_msc=p.time_msc,
                    time=p.time_msc // 1000,
                )
            )
        return result

    tape.mt.history_deals_get = lambda *a, ticket=None: [
        z for z in deals if ticket is None or z.ticket == ticket
    ]
    tape.mt.order_send = partial
    tape.run()
    assert [r["volume"] for r in tape.events("intrabar_partial_guarded")] == [0.04]
    assert (
        len(tape.mt.requests) == 2
        and not tape.mt.positions
        and tape.mt.requests[-1]["volume"] == 0.04
    )
    assert tape.state()["manual_hold"] and not tape.state().get("ladder_by_ticket")
    assert [x["outcome"] for x in tape.events("label_decision")] == [
        "uncertain_or_partial_fill_manual_hold",
        "manual_reconciliation_required",
    ]


def test_current_swing_not_an_older_guard_or_later_pivot():
    from test_hull_failed_mss import P, bars

    # Exhaust every prefix through initial trend, continuation and two shifts.
    for side in (-1, 1):
        h = Hull(3, P, 0.01, tick_size=0.025)
        current = None
        found = False
        for b in bars(side, 3):
            v = h.update(b).structure
            if current is None or v.bias != current.bias:
                assert v.range_swing is None
            if v.range_swing:
                assert v.range_swing.bar_s < v.range_swing.known_s <= v.closed_s
                if v.range_swing != v.guard:
                    found = True
                    assert (
                        (v.range_swing == v.pl and v.lo_class == 1)
                        or (v.range_swing == v.ph and v.hi_class == -1)
                        or current.range_swing == v.range_swing
                    )
            current = v
        assert (
            found
        )  # defended reversal guard and the current pullback are not synonyms


def test_stop_and_entry_vetoes_still_run_on_a_genuine_recross(tmp_path, monkeypatch):
    tape = Tape(
        tmp_path, monkeypatch, [(100, OFF), (200, ON), (300, OFF), (400, ON), (500, ON)]
    )
    original = tape.mt.symbol_info_tick

    def spread(name):
        q = original(name)
        if tape.i >= 3:
            q.ask = q.bid + 1
        return q

    tape.mt.symbol_info_tick = spread
    tape.run()
    assert [e["outcome"] for e in tape.events("label_decision")] == [
        "demo_batch_sent",
        "spread_cap",
    ]
    assert len(tape.events("demo_result")) == 4 and not tape.mt.positions


def test_unresolved_fill_never_adopted_by_comment_or_symbol_alone(
    tmp_path, monkeypatch
):
    tape = Tape(tmp_path, monkeypatch, [(100, OFF), (200, ON), (300, OFF), (400, ON)])
    send = tape.mt.order_send

    def unknown(req):
        send(req)

    tape.mt.order_send = unknown
    tape.run()
    assert len(tape.mt.positions) == 1 and len(tape.mt.requests) == 1
    state = tape.state()
    assert state["manual_hold"] and not state["hull_lane_positions_v1"]
    assert not tape.events("exit_request")


def test_original_close_repair_by_date_never_substitutes_successor(
    tmp_path, monkeypatch
):
    from hull_cross_tape import as_rate

    from bot.hull.feed import Stream

    tape = Tape(tmp_path, monkeypatch, [(100, OFF), (200, ON)])
    h = HullConfig.model_validate(tape.cfg["hull"])
    lane = Stream(3, h.indicator, 0.01, tick_size=0.025)
    path = prefix(3) + [Bar(T, 2006.2, 2010, 2005.9, 2005.9)]

    def exact(_name, tf, at, n):
        assert at.timestamp() == T and tf == 3
        return [as_rate(b) for b in path[-n:]]

    tape.mt.copy_rates_from = exact
    assert lane.original_close(tape.mt, NAME, T + 180) is None  # still index zero
    tape.mt.copy_rates_from_pos = lambda _n, _tf, pos, count: (
        [as_rate(path[-1])] if pos == 1 else []
    )
    close = lane.original_close(tape.mt, NAME, T + 180)
    assert relation(close, "low", 1) is False and not lane.h.n
    path[-1] = replace(path[-1], t=T + 180)
    assert lane.original_close(tape.mt, NAME, T + 180) is None


def test_simultaneous_opposite_and_incomplete_healthy_peer_skip(tmp_path, monkeypatch):
    from bot.hull.feed import Stream

    # Inject two synchronized signal readings solely to isolate arbitration.
    tape = Tape(tmp_path, monkeypatch, [(100, OFF), (200, ON)], parents=(5, 15))
    tape.paths[10] = prefix(10, -1)
    tape.paths[15] = prefix(15, -1)
    original = Stream.project

    def project(lane, mt, name, q):
        raw = original(lane, mt, name, q)
        if raw is None:
            return None
        if lane.tf == 10 and tape.i == 1:
            return replace(
                raw, label=-1, a=raw.b + 0.1, a_high=raw.b + 0.1, cross_high=-1
            )
        return replace(raw, label=0, cross_high=0) if lane.tf == 10 else raw

    monkeypatch.setattr(Stream, "project", project)
    # Both stops must be on the right side of the common bid.
    tape.paths[10] = [
        Bar(b.t, b.o + 12, b.h + 12, b.l + 12, b.c + 12) for b in tape.paths[10]
    ]
    tape.run()
    (r,) = tape.events("intrabar_arbitration")
    assert r["outcome"] == "conflicting_simultaneous_pairs" and not tape.mt.requests
    tape.bad = lambda tf, pos: tf == 10 and pos == 0 and tape.i == 1
    tape.run(start=0, armed=False)
    (r,) = tape.events("intrabar_arbitration", shadow=True)
    assert r["outcome"] == "intrabar_data_unverified"


def test_two_recrosses_one_second_remain_separate_in_real_journal_import(
    tmp_path, monkeypatch
):
    from contextlib import closing

    monkeypatch.syspath_prepend(str(ROOT / "HullJournal"))
    monkeypatch.syspath_prepend(str(ROOT / "HullJournal/tests"))
    from fixtures import FakeMT, deal
    from ledger import db_open, iso, make_views, sync

    tape = Tape(
        tmp_path,
        monkeypatch,
        [(100, OFF), (200, ON), (300, OFF), (400, ON), (500, OFF)],
        side=-1,
    )
    tape.run()
    rows = tape.events("demo_result")
    assert len(rows) == 8
    broker = FakeMT([], T + 180)
    broker.login, broker.host = 1, "t"
    for r in rows:
        did = r["deal"]
        entry = deal(
            did,
            did,
            T,
            1,
            0,
            0.1,
            r["price"],
            ms=r["trigger_msc"] % 1000 + 5,
            comment="broker rewrote the tag",
        )
        entry.order = r["opening_order"]
        broker.ds.extend(
            [
                entry,
                deal(
                    did + 100,
                    did,
                    T,
                    0,
                    1,
                    0.1,
                    r["price"] + 0.1,
                    ms=r["trigger_msc"] % 1000 + 100,
                    profit=-1,
                ),
            ]
        )
    with closing(db_open(":memory:")) as c:
        sync(c, broker, broker.at, repo=tmp_path)
        data = make_views(c)[0]
        assert len(data["tickets"]) == 8 and len(data["setups"]) == 2
        assert {r["ticket_count"] for r in data["setups"]} == {"4"}
        for r in data["setups"] + data["tickets"]:
            assert (
                r["entry_timing"] == "intrabar_pair_cross"
                and r["setup_type"] == "intrabar_pair_swing"
                and r["swing_kind"] == "LH"
            )
            assert r["swing_price"] == str(tape.px(1999.6)) and r[
                "signal_bar_end_utc"
            ] == iso((T + 180) * 1000)
            assert r["signal_trigger_utc"] in (iso(T * 1000 + 200), iso(T * 1000 + 400))
            assert (
                not r["signal_close_utc"]
                and not r["cross_candle_wick"]
                and not r["frozen_cross_sl"]
            )
        assert all(
            float(r["initial_sl"]) - float(r["raw_chart_sl"]) == pytest.approx(0.05)
            for r in data["tickets"]
        )
        assert all(r["trigger_to_submit_ms"] == "0" for r in data["tickets"])


def test_single_market_default_depth_and_ordinary_mss_skip_then_same_bar_entry(
    tmp_path, monkeypatch
):
    # No independent fake chart directions: both native histories aggregate the
    # very same minute tape. The first qualifying tick spends a real parent MSS skip.
    tape = Tape(
        tmp_path,
        monkeypatch,
        [(100, 2012.7), (200, 2016.6), (300, 2012.7), (400, 2016.6)],
    )
    m = []
    rng = Random(18)
    px = 2000.0
    for i in range(600):
        op = px
        px = round(op + rng.choice([-1.1, -0.7, -0.3, 0.3, 0.7, 1.1]), 2)
        m.append(
            Bar(
                T + (i - 600) * 60,
                op,
                round(max(op, px) + rng.randrange(1, 4) * 0.1, 2),
                round(min(op, px) - rng.randrange(1, 4) * 0.1, 2),
                px,
            )
        )

    def agg(bars):
        return Bar(
            bars[0].t,
            bars[0].o,
            max(b.h for b in bars),
            min(b.l for b in bars),
            bars[-1].c,
        )

    tape.paths = {
        tf: [agg(m[i : i + tf]) for i in range(0, len(m), tf)] for tf in (3, 5)
    }
    assert tape.paths[3][-1].c == tape.paths[5][-1].c == 2013
    tape.run()
    first, recross = tape.events("intrabar_arbitration")
    assert (
        first["candidates"][0]["reason"] == "first_trend_cross_after_mss"
        and not first["owner_tf"]
    )
    assert recross["owner_tf"] == 5 and recross["trigger_msc"] == T * 1000 + 400
    ev = tape.events("demo_result")
    assert len(ev) == 4
    assert all(
        r["swing_price"] == 2008.6
        and r["swing_known_s"] == T - 1980
        and r["trigger_msc"] == T * 1000 + 400
        for r in ev
    )


def test_exact_touch_latches_exit_even_with_no_opposing_label(tmp_path, monkeypatch):
    from bot.hull.feed import Stream

    tape = Tape(tmp_path, monkeypatch, [(100, OFF), (200, ON), (300, ON)])
    original = Stream.project

    def touch(lane, mt, name, q):
        r = original(lane, mt, name, q)
        return (
            replace(r, b=r.a, label=0, cross_low=0)
            if tape.i == 2 and lane.tf == 3
            else r
        )

    monkeypatch.setattr(Stream, "project", touch)
    tape.run()
    assert not tape.mt.positions
    assert {e["reason"] for e in tape.events("exit_request")} == {
        "origin_cross_uncrossed"
    }


def test_invalid_saved_origin_never_interpreted_as_a_closed_cross(
    tmp_path, monkeypatch
):
    tape = Tape(tmp_path, monkeypatch, [(100, OFF), (200, ON), (300, OFF)])
    tape.run(count=2)
    state = tape.state()
    next(iter(state["hull_lane_positions_v1"].values()))["intrabar_origin"]["end"] += (
        600
    )
    next(tmp_path.glob(".moneyprinterbot/hull/*/state.json")).write_text(
        json.dumps(state)
    )
    with pytest.raises(DemoSafetyError, match="unverified cross identity"):
        tape.run(start=2)
    assert len(tape.mt.requests) == 4
