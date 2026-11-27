"""Paired market routing; actual Hull streams + deterministic native candles, fake MT5 only."""

import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from test_hull_direct_retest import make as make_retest
from test_hull_intrabar_market import NAME, OFF, ON, T, Tape, raw_candidate

from bot.hull.bar import cross_location
from bot.hull.config import PAIR_POLICY, REGION_BASIS
from bot.hull.intrabar_signal import candidate
from bot.hull.orders import Grid, spread_adjusted_stop
from bot.hull.ownership import Owners


def sltp(tape):
    return [r for r in tape.mt.requests if r["action"] == tape.mt.TRADE_ACTION_SLTP]


@pytest.mark.parametrize("side", [-1, 1])
def test_outside_on_bracket_from_first_leg_no_defer_or_second_spread(
    tmp_path, monkeypatch, side
):
    # The native candle gaps on open. The two Hull segments intersect outside
    # its wick. Passing final OHLC to the entry would be a look-ahead error.
    tape = Tape(
        tmp_path,
        monkeypatch,
        [(100, 2007.3), (200, 2007.8), (180100, 2007.8)],
        side=side,
    )
    tape.origin_open = tape.px(2007.3)
    tape.run()
    rows = tape.events("demo_result")
    assert len(rows) == 4 and not sltp(tape) and not tape.events("exit_request")
    grid = Grid.from_info(tape.mt.i)
    for r in rows:
        cross = r["hull_cross_price"]
        low, high = sorted((tape.origin_open, tape.px(2007.8)))
        assert (r["cross_signal_low"], r["cross_signal_high"]) == (low, high)
        assert not low <= cross <= high
        raw = grid.price(grid.index(cross, side == -1))
        wanted, _ = spread_adjusted_stop(grid, side, raw, r["send_bid"], r["send_ask"])
        assert (
            r["cross_region_basis"] == REGION_BASIS
            and r["cross_region_location"] == "outside"
        )
        assert r["request"]["sl"] == wanted != r["request"]["price"]
        assert side * (raw - r["frozen_swing_sl"]) > 0 and r["raw_chart_sl"] == raw
        assert r["cross_region_stop_status"] == "active"
    assert all(
        v["intrabar_origin"]["status"] == "confirmed"
        for v in tape.state()["hull_lane_positions_v1"].values()
    )
    assert not tape.events("closed_label") and not tape.events(
        "cross_region_stop_activation"
    )


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("favorable", [False, True])
def test_inside_close_vs_frozen_level_even_while_curves_still_hold(
    tmp_path, monkeypatch, side, favorable
):
    tape = Tape(
        tmp_path,
        monkeypatch,
        [(100, 2005.0), (200, ON), (180100, ON), (180200, ON)],
        side=side,
    )
    # Unseen last native print. The next candle's tick goes back in favor.
    tape.hidden = [(179990, ON if favorable else 2007.2)]
    tape.run()
    rows = tape.events("demo_result")
    assert len(rows) == 4 and not tape.events("closed_label")
    cross = rows[0]["hull_cross_price"]
    assert all(
        r["cross_signal_low"] <= cross <= r["cross_signal_high"]
        and r["cross_region_location"] == "inside"
        and r["raw_chart_sl"] == r["frozen_swing_sl"]
        and r["cross_region_stop_status"] == "awaiting_origin_close"
        for r in rows
    )
    (event,) = tape.events("origin_cross_check")
    assert event["phase"] == "final_close" and event["candle_close"] == tape.px(
        tape.hidden[0][1]
    )
    assert (side * (event["candle_close"] - cross) > 0) == favorable
    if not favorable:
        assert list(event["tickets"].values()) == ["invalid"] * 4
        assert (
            len(tape.events("lane_exit_confirmed"))
            == len(tape.events("exit_request"))
            == 4
        )
        assert not tape.mt.positions and not sltp(tape)
        return
    assert list(event["tickets"].values()) == ["confirmed"] * 4
    assert len(sltp(tape)) == 4 and not tape.events("exit_request")
    for pos, r in zip(tape.mt.positions, rows, strict=True):
        mgr = tape.state()["ladder_by_ticket"][str(pos.ticket)]
        assert (
            mgr["initial_sl"] == r["request"]["sl"]
            and side * (pos.sl - mgr["initial_sl"]) > 0
        )
        assert (mgr["sl"], mgr["tp"], mgr["rung"]) == (pos.sl, r["request"]["tp"], 0)
        assert pos.sl == pytest.approx(
            r["cross_region_raw_stop"] + (0.05 if side == -1 else 0)
        )
        assert not mgr.get("pending") and not mgr.get("hold")


@pytest.mark.parametrize("side", [-1, 1])
def test_wick_edges_inclusive_wrong_side_and_touch_never_enter(side):
    r, lane = raw_candidate(side)
    form = r.forming_bar
    boundary = form.l if side == 1 else form.h
    for price, location, ok in [
        (boundary, "inside", True),
        (boundary - side * 0.01, "outside", True),
        (r.trigger.bid, "wrong_side", False),
        (r.trigger.bid + side * 0.01, "wrong_side", False),
    ]:
        read = candidate(replace(r, cross_price=price), 5, r.structure, lane, T + 0.15)
        assert read.region == location and bool(read.entry_side) == ok
    assert cross_location(side, None, r.trigger.bid, form.l, form.h) == "unverified"
    assert not candidate(
        replace(r, forming_bar=None), 5, r.structure, lane, T + 0.15
    ).entry_side


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("move", ["after_check", "after_first_fill"])
def test_one_favorable_tick_keeps_origin_and_checks_each_remaining_leg(
    tmp_path, monkeypatch, side, move
):
    tape = Tape(
        tmp_path, monkeypatch, [(100, OFF), (200, ON), (240, ON + 0.025)], side=side
    )
    checked, sent = [], []
    check, send = tape.mt.order_check, tape.mt.order_send

    def inspect(req):
        checked.append(dict(req))
        res = check(req)
        if move == "after_check" and tape.i == 1:
            tape.i = 2
        return res

    def send_one(req):
        assert req in checked  # a changed price gets its own exact broker check
        sent.append(dict(req))
        res = send(req)
        if move == "after_first_fill" and tape.i == 1:
            tape.i = 2
        return res

    tape.mt.order_check, tape.mt.order_send = inspect, send_one
    tape.run(count=2)
    rows = tape.events("demo_result")
    assert len(sent) == len(rows) == 4
    assert {r["trigger_msc"] for r in rows} == {T * 1000 + 200}
    assert len({r["hull_cross_price"] for r in rows}) == 1
    assert all(r["trigger_bid"] == tape.px(ON) for r in rows)
    assert rows[-1]["send_bid"] == tape.px(ON + 0.025) and not tape.events(
        "exit_request"
    )


@pytest.mark.parametrize("side", [-1, 1])
def test_limit_geometry_blocks_touch_or_wrong_side_even_with_market_fallback(
    tmp_path, monkeypatch, side
):
    mt, desk, r, logs, _ = make_retest(
        tmp_path,
        monkeypatch,
        side=side,
        hook=lambda cfg: cfg["hull"].update(retest_market_fallback=True),
    )
    # Retest is 0.12 towards its wick; symbol tick is 0.01. Use a cross
    # between that limit and the closed Bid. The bracket is never submitted.
    outcome = desk.batch(NAME, replace(r, cross_price=round(r.close - side * 0.10, 2)))
    assert (
        outcome == "cross_region_invalid_at_limit" and not mt.orders and not mt.requests
    )
    assert not any(v["event"] == "retest_market_fallback" for v in logs)


def closed_tape(tmp_path, mp, side, *after):
    tape = Tape(tmp_path, mp, [(179650, OFF), (179850, OFF), *after], side=side)
    tape.hidden = [
        (179990, ON)
    ]  # first seen as a completed label, never a sampled live edge
    return tape


@pytest.mark.parametrize("side", [-1, 1])
def test_default_accepts_fresh_closed_only_but_never_replays_it(
    tmp_path, monkeypatch, side
):
    tape = closed_tape(
        tmp_path, monkeypatch, side, (180100, ON), (180250, ON), (180400, ON)
    )
    tape.run(count=4)
    rows = tape.events("demo_result")
    assert len(rows) == 4 and not tape.events("intrabar_pair_cross")
    assert all(
        r["entry_policy"] == PAIR_POLICY
        and r["entry_side"] == side
        and r["entry_timing"] == "confirmed_close"
        and r["trigger_msc"] == (T + 180) * 1000
        and r["request"]["comment"].startswith("p5x3_")
        for r in rows
    )
    assert tape.state()["pair_consumed"][NAME] == T + 180
    tape.mt.positions.clear()  # confirmed flat on reattach, same closed label still in history
    tape.run(start=4)
    assert len(tape.events("demo_result")) == 4


def seed_opposite(tape):
    # This is a previously reconciled, older paired batch, not an invented
    # signal. Record it with the real ownership/fingerprint code and same config.
    tape.run(count=1)
    path = next(tape.root.glob(".moneyprinterbot/hull/*/state.json"))
    state = json.loads(path.read_text())
    (policy,) = state["pair_parent_first_cross"]
    for key in (701, 702):
        p = NS(
            ticket=key,
            identifier=key + 1000,
            type=1 if tape.side == 1 else 0,
            volume=0.1,
            price_open=tape.px(2007.0),
            profit=0.0,
            symbol=NAME,
            magic=tape.cfg["hull"]["magic"],
            sl=tape.px(2025.0),
            tp=tape.px(1990.0),
            time_msc=(T - 60) * 1000,
        )
        assert Owners(state).bind(p, 5, pair_child=3, policy_key=policy)
        tape.mt.positions.append(p)
    path.write_text(json.dumps(state))


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize(
    "case",
    [
        "within",
        "expires",
        "restart",
        "spread",
        "fades",
        "no_first_quote",
        "unlinked_out",
    ],
)
def test_opposite_drain_retains_only_original_closed_window(
    tmp_path, monkeypatch, side, case
):
    release = 189000 if case == "expires" else 182000
    tape = closed_tape(
        tmp_path,
        monkeypatch,
        side,
        (180100, ON),
        (180500, ON if case != "fades" else OFF),
        (release, ON),
        (release + 250, ON),
    )
    seed_opposite(tape)
    pending = set()
    get, send, tick = (
        tape.mt.positions_get,
        tape.mt.order_send,
        tape.mt.symbol_info_tick,
    )

    def inventory(ticket=None):
        if tape.i >= 4 and pending:
            for p in list(tape.mt.positions):
                if p.ticket in pending:
                    tape.mt.positions.remove(p)
                    pending.remove(p.ticket)
                    # Server OUT on the opposing position, possibly not the opening magic.
                    tape.mt.deals.append(
                        NS(
                            symbol=NAME,
                            magic=0,
                            ticket=p.ticket + 100,
                            position_id=p.identifier
                            if case != "unlinked_out"
                            else 99999,
                            time_msc=int(tape.clock() * 1000),
                            time=int(tape.clock()),
                            entry=1,
                            type=1 - p.type,
                        )
                    )
        return get(ticket=ticket)

    def delayed(req):
        if req.get("position") == 702:
            assert req["action"] == tape.mt.TRADE_ACTION_DEAL and not pending
            pending.add(702)
            tape.mt.requests.append(dict(req))
            return NS(retcode=10009)  # request accepted, NOT proof of flatness
        if not req.get("position"):
            assert not pending and not any(
                p.ticket in (701, 702) for p in tape.mt.positions
            )
        return send(req)

    def quote(name):
        if case == "no_first_quote" and tape.i == 2:
            return None
        q = tick(name)
        if case == "spread" and tape.i >= 4:
            q.ask += 2.0
        return q

    tape.mt.positions_get, tape.mt.order_send, tape.mt.symbol_info_tick = (
        inventory,
        delayed,
        quote,
    )
    if case == "restart":
        tape.run(start=1, count=3)
        tape.run(start=4)
    else:
        tape.run(start=1)
    waits = tape.events("pair_wait_opposite_flat")
    rows = tape.events("demo_result")
    assert len(waits) == 1 and waits[0]["deadline_s"] == T + 180 + 8
    assert [r["ticket"] for r in tape.events("exit_request")] == [701, 702]
    assert {r["ticket"] for r in tape.events("lane_exit_confirmed")} == {701, 702}
    assert not pending and tape.state()["pair_consumed"][NAME] == T + 180
    if case in ("within", "no_first_quote"):
        assert len(rows) == 4 and {r["send_tick_time_msc"] for r in rows} == {
            T * 1000 + release
        }
        assert {r["entry_policy"] for r in rows} == {PAIR_POLICY}
    else:
        assert not rows and not tape.mt.positions


def test_unknown_ack_readback_uses_sent_bracket_not_new_sell_spread(
    tmp_path, monkeypatch
):
    tape = Tape(
        tmp_path,
        monkeypatch,
        [(100, OFF), (200, ON), (180100, ON), (180200, ON), (180400, ON)],
        side=-1,
    )
    send, tick = tape.mt.order_send, tape.tick

    def delay(req):
        if req.get("position") == 1 and req["action"] == 6:
            tape.mt.requests.append(dict(req))
            return None
        return send(req)

    def narrower(name):
        q = tick(name)
        if tape.i >= 3:
            q.ask = round(q.bid + 0.025, 3)
        return q

    tape.mt.order_send, tape.mt.symbol_info_tick = delay, narrower
    tape.run(count=3)

    def guard():
        return tape.state()["hull_lane_positions_v1"]["1"]["intrabar_origin"]

    intent = guard()["cross_stop_intent"]
    assert guard()["cross_region_status"] == "unknown_ack"
    pos = tape.mt.positions[0]
    pos.sl, pos.tp = (
        intent["sl"],
        intent["tp"] - 0.025,
    )  # only ONE matching leg of the bracket
    tape.run(start=3, count=1)
    assert guard()["cross_region_status"] == "unknown_ack" and not tape.state()[
        "ladder_by_ticket"
    ]["1"].get("hold")
    pos.tp = intent["tp"]
    tape.run(start=4)
    assert (
        guard()["cross_region_status"] == "active"
        and "cross_stop_intent" not in guard()
    )
    assert intent["sl"] == tape.state()["ladder_by_ticket"]["1"]["sl"]
    assert len([r for r in sltp(tape) if r["position"] == 1]) == 1


@pytest.mark.parametrize(
    "case",
    [
        "stronger_managed",
        "changed_between_checks",
        "foreign_between_checks",
        "grid_changed",
        "refused",
    ],
)
def test_cross_activation_respects_prior_protection_and_ticket_identity(
    tmp_path, monkeypatch, case
):
    tape = Tape(
        tmp_path, monkeypatch, [(100, OFF), (200, ON), (180100, ON), (180300, ON)]
    )
    tape.run(count=2)
    pos = tape.mt.positions[0]
    raw = tape.events("demo_result")[0]["cross_region_raw_stop"]
    stronger = round(raw + 0.25, 3)
    path = next(tape.root.glob(".moneyprinterbot/hull/*/state.json"))
    if case == "stronger_managed":
        pos.sl = stronger
        state = json.loads(path.read_text())
        state["ladder_by_ticket"]["1"]["sl"] = stronger
        path.write_text(
            json.dumps(state)
        )  # prior acknowledged SL, unchanged TP/risk basis
    if case == "grid_changed":
        tape.mt.i.trade_tick_size = 0.05
    targets = []
    check, send = tape.mt.order_check, tape.mt.order_send

    def checking(req):
        result = check(req)
        if req["action"] == 6 and req["position"] == 1:
            if case == "foreign_between_checks":
                pos.magic = 77
            if case == "changed_between_checks":
                pos.sl = stronger
                pos.tp += 0.05
        return result

    def sendret(req):
        if req["action"] == 6 and req["position"] == 1:
            targets.append(tape.i)
            if case == "refused" and tape.i == 2:
                tape.mt.requests.append(dict(req))
                return NS(retcode=10016)
        return send(req)

    tape.mt.TRADE_RETCODE_INVALID_STOPS = 10016
    tape.mt.order_check, tape.mt.order_send = checking, sendret
    tape.run(start=2)
    rec = tape.state()["hull_lane_positions_v1"]["1"]["intrabar_origin"]
    if case == "refused":
        assert (
            targets == [2, 3]
            and rec["cross_region_status"] == "active"
            and pos.sl == raw
        )
    else:
        assert targets == [] and not any(
            r.get("position") == 1 for r in tape.mt.requests
        )
        if case == "stronger_managed":
            assert rec["cross_region_status"] == "retained" and pos.sl == stronger
        elif case == "changed_between_checks":
            assert (
                pos.sl == stronger
                and tape.state()["ladder_by_ticket"]["1"]["hold"]
                == "ladder_external_levels_review"
            )
        else:
            assert rec["cross_region_status"] == "pending"


@pytest.mark.parametrize("side", [-1, 1])
def test_exact_origin_close_touch_reserves_exit_even_when_lines_hold(
    tmp_path, monkeypatch, side
):
    # Feed the *same numeric value*, not a grid-rounded approximation. A
    # synthetic exact touch exercises equality without changing the saved guard.
    tape = Tape(tmp_path, monkeypatch, [(100, OFF), (200, ON), (180100, ON)], side=side)
    tape.run(count=2)
    cross = tape.events("demo_result")[0]["hull_cross_price"]
    px = tape.px
    tape.px = lambda value: cross if value is None else px(value)
    tape.hidden = [(179999, None)]
    tape.run(start=2)
    assert (
        not tape.mt.positions
        and len(tape.events("lane_exit_confirmed")) == 4
        and not sltp(tape)
    )
    assert tape.events("origin_cross_check")[-1]["candle_close"] == cross


@pytest.mark.parametrize(
    "case",
    [
        "opposite_child",
        "wrong_frozen_side",
        "new_source",
        "forged_origin",
        "missing_cross",
        "more_than_one_tick",
    ],
)
def test_reverification_is_not_permission_to_trade_another_episode(
    tmp_path, monkeypatch, case
):
    from bot.hull.feed import Stream
    from bot.hull.intrabar_book import IntrabarBook

    tape = Tape(tmp_path, monkeypatch, [(100, OFF), (200, ON), (240, ON + 0.025)])
    if case == "more_than_one_tick":
        tape.timeline[2] = (240, ON + 0.05)
    if case == "opposite_child":
        tape.timeline[2] = (240, OFF)
    if case == "wrong_frozen_side":
        tape.timeline[2] = (240, 2007.2)
    old = Stream.project

    def project(self, mt, name, tick):
        r = old(self, mt, name, tick)
        if r and self.tf == 3 and tape.i == 2:
            if case == "new_source":
                r = replace(r, active_source="high")
            if case == "missing_cross":
                r = replace(r, cross_price=None, cross_fraction=None)
        return r

    monkeypatch.setattr(Stream, "project", project)
    verify = IntrabarBook.verify

    def forged(self, desk, name, r, q, clock):
        if case == "forged_origin" and tape.i == 2:
            r = replace(r, cross_price=r.cross_price - 0.5)
        return verify(self, desk, name, r, q, clock)

    monkeypatch.setattr(IntrabarBook, "verify", forged)
    check = tape.mt.order_check

    def advance(req):
        res = check(req)
        if tape.i == 1:
            tape.i = 2
        return res

    tape.mt.order_check = advance
    tape.run(count=2)
    assert (
        not tape.mt.requests
        and not tape.events("demo_result")
        and not tape.mt.positions
    )


def test_newer_live_pair_and_distinct_actionable_close_at_same_poll_do_not_stack(
    tmp_path, monkeypatch
):
    # A hidden final print completes M3; M5 (another parent's trigger) is
    # still forming and crosses on the first observed tick after the M3 close.
    tape = Tape(
        tmp_path,
        monkeypatch,
        [(179650, OFF), (179850, OFF), (180100, ON), (180250, ON)],
        parents=(5, 10),
    )
    tape.hidden = [(179990, ON)]
    tape.run()
    assert not tape.events("demo_result") and not tape.mt.requests
    assert (
        tape.events("intrabar_arbitration")[-1]["outcome"] == "competing_tick_and_close"
    )
    assert tape.state()["paired_arbitration_through"][NAME] == T + 180


@pytest.mark.parametrize("side", [-1, 1])
def test_closed_label_published_outside_original_window_never_admits(
    tmp_path, monkeypatch, side
):
    tape = closed_tape(tmp_path, monkeypatch, side, (189100, ON), (189250, ON))
    tape.run()
    assert (
        not tape.mt.requests
        and not tape.events("demo_result")
        and not tape.events("pair_wait_opposite_flat")
    )
    assert tape.events("pair_arbitration")[-1]["outcome"] == "entry_window_expired"
