"""Real paired streams; only the in-process fabricated broker is exercised."""

import pytest
from test_hull_intrabar_market import OFF, ON, T, Tape


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("filled", [1, 2])
@pytest.mark.parametrize("closed", [False, True, "explicit"])
def test_repeated_valid_ticks_finish_only_the_approved_unsent_legs(
    tmp_path, monkeypatch, side, filled, closed
):
    offset = 180000 if closed else 0
    timeline = [(100, OFF), (offset + 200, ON)] + [
        (offset + 220 + n * 20, ON + shift)
        for n, shift in enumerate((0.100, -0.075, 0.050, -0.025))
    ]
    tape = Tape(tmp_path, monkeypatch, timeline, side=side)
    if closed == "explicit":
        tape.cfg["hull"]["entry_timing"] = "confirmed_cross_next_bar"
    if closed:
        tape.hidden = [(179999, ON)]
    tape.cfg["hull"]["symbols"]["XAUUSD"]["target_usd"] = 17.25
    check, send = tape.mt.order_check, tape.mt.order_send
    checked = []

    def inspect(req):
        checked.append(dict(req))
        result = check(req)
        if len(tape.mt.positions) == filled and tape.i < len(timeline) - 1:
            tape.i += 1  # new quote between broker check and pre-send; can retrace without invalidation
        return result

    def submit(req):
        q = tape.tick(None)
        assert req == checked[-1] and req["price"] == (q.ask if side == 1 else q.bid)
        return send(req)

    tape.mt.order_check, tape.mt.order_send = inspect, submit
    tape.run(count=2)
    receipts = tape.events("demo_result")
    assert [r["leg"] for r in receipts] == [1, 2, 3, 4]
    assert len(checked) == 8 and len(tape.mt.positions) == len(tape.mt.requests) == 4
    assert len({r["request"]["comment"] for r in receipts}) == 4
    assert (
        len({r["signal"] for r in receipts})
        == len({r["raw_chart_sl"] for r in receipts})
        == 1
    )
    assert receipts[0]["signal"] == (T + 180 if closed else T + 0.2)
    assert receipts[filled]["send_tick_time_msc"] == T * 1000 + timeline[-1][0]
    assert all(
        r["configured_target_usd"] == 17.25
        and r["initial_goal_usd"] == pytest.approx(17.25 + r["entry_spread_usd"])
        for r in receipts
    )
    assert [r["outcome"] for r in tape.events("label_decision")] == ["demo_batch_sent"]
    assert not tape.state().get("manual_hold")


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize(
    "case", ["valid", "missing_peer", "future_wick", "touch", "same_ms", "expired"]
)
def test_tick_advancing_between_two_chart_reads_needs_an_unbroken_witness(
    tmp_path, monkeypatch, side, case
):
    from dataclasses import replace

    from bot.hull.feed import Stream

    stamp = 200 if case == "same_ms" else 9200 if case == "expired" else 240
    price = 2007.18 if case == "touch" else ON + 0.125
    tape = Tape(
        tmp_path, monkeypatch, [(100, OFF), (200, ON), (stamp, price)], side=side
    )
    old = Stream.project

    def project(self, mt, name, tick):
        raw = old(self, mt, name, tick)
        if len(mt.positions) == 2 and tick.ms == T * 1000 + 200:
            if self.tf == 3 and tape.i == 1:
                tape.i = 2  # inside verify after fill, NOT between two complete plans
            if self.tf == 5:
                if case == "missing_peer":
                    return None
                if case == "future_wick":
                    return replace(
                        raw,
                        forming_bar=replace(raw.forming_bar, l=raw.forming_bar.l - 1),
                    )
        return raw

    monkeypatch.setattr(Stream, "project", project)
    tape.run(count=2)
    receipts = tape.events("demo_result")
    assert len(receipts) == (4 if case == "valid" else 2)
    if case == "valid":
        assert receipts[2]["send_bid"] == tape.px(price)
    assert not tape.state().get("manual_hold")


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize(
    "case",
    [
        "quote_refused",
        "same_refused",
        "other_refusal",
        "risk",
        "margin",
        "kill",
        "wide",
        "changed_contract",
        "no_ack",
        "expired",
    ],
)
def test_once_a_gate_fails_no_unsent_sibling_or_later_topup(
    tmp_path, monkeypatch, side, case
):
    from types import SimpleNamespace as NS

    times = [
        (100, OFF),
        (200, ON),
        (260, ON + 0.15),
        (300, ON + 0.175),
        (340, ON + 0.175),
    ]
    if case == "expired":
        # Advance in multiple sub-quote-age steps: no quote stale, but the signal expires.
        times[2:] = [
            (3200, ON + 0.1),
            (6200, ON + 0.2),
            (8250, ON + 0.3),
            (8300, ON + 0.3),
        ]
    tape = Tape(tmp_path, monkeypatch, times, side=side)
    if case == "risk":
        # 4-leg initial risk fits. A much more expensive residual cannot be sent.
        tape.cfg["hull"]["limits"]["max_open_risk_usd"] = 428
        tape.timeline[2] = (260, ON + 2.5)
    orig_tick = tape.mt.symbol_info_tick

    def tick(name):
        q = orig_tick(name)
        if case == "wide" and tape.i >= 2:
            q.ask += 1
        return q

    tape.mt.symbol_info_tick = tick
    check = tape.mt.order_check

    def inspect(req):
        res = check(req)
        if len(tape.mt.positions) == 2 and tape.i == 1:
            if case != "same_refused":
                tape.i += 1
            if case in ("quote_refused", "same_refused", "other_refusal"):
                return NS(
                    retcode=10019 if case == "other_refusal" else 10020, margin_free=0
                )
            if case == "margin":
                tape.mt.a.margin_free = 0.001
            elif case == "kill":
                (tmp_path / "KILL").touch()
            elif case == "changed_contract":
                tape.mt.i.trade_tick_size = 0.05
            elif case == "no_ack":
                tape.mt.bad = True
        elif case == "expired" and len(tape.mt.positions) == 2 and tape.i < 4:
            tape.i += 1
        return res

    tape.mt.order_check = inspect
    tape.run(count=2)
    (result,) = tape.events("label_decision")
    opens = [r for r in tape.mt.requests if not r.get("position")]
    if case == "quote_refused":
        assert len(opens) == 4 and result["outcome"] == "demo_batch_sent"
        assert tape.events("demo_result")[2]["unsent_reprices"] == 1
    else:
        assert len(opens) == (3 if case == "no_ack" else 2)
        assert result["outcome"] != "demo_batch_sent"
        if case == "no_ack":
            assert tape.state()["manual_hold"]["leg"] == 3
        else:
            assert result["outcome"].startswith(
                "protected_partial_no_topup:"
            ) and not tape.state().get("manual_hold")
        if case == "risk":
            assert result["outcome"].endswith("portfolio_risk_limit")
        if case == "expired":
            assert result["outcome"].endswith("entry_window_expired")
        # No duplicate sends: neither a returning quote nor a restart resumes a partial reservation.
        if case not in ("kill", "changed_contract", "no_ack"):
            tape.run(start=len(times) - 1, count=1)
            assert [r for r in tape.mt.requests if not r.get("position")] == opens


@pytest.mark.parametrize("side", [-1, 1])
def test_final_quote_advances_and_wider_valid_spread_prices_only_the_unsent_ticket(
    tmp_path, monkeypatch, side
):
    from bot.hull.desk import Desk

    tape = Tape(
        tmp_path,
        monkeypatch,
        [(100, OFF), (200, ON), (240, ON + 0.125), (280, ON - 0.1)],
        side=side,
    )
    plain_tick = tape.mt.symbol_info_tick

    def tick(name):
        q = plain_tick(name)
        if tape.i >= 2:
            q.ask = round(q.bid + (0.125 if tape.i == 2 else 0.1), 4)
        return q

    tape.mt.symbol_info_tick = tick
    entry_quote = Desk._entry_quote

    def advancing(self, name, window, stage):
        if stage == "final_send" and len(tape.mt.positions) == 2 and tape.i < 3:
            tape.i += 1
        return entry_quote(self, name, window, stage)

    monkeypatch.setattr(Desk, "_entry_quote", advancing)
    tape.run(count=2)
    rows = tape.events("demo_result")
    assert len(rows) == 4 and [r["unsent_reprices"] for r in rows] == [0, 0, 2, 0]
    assert [r["initial_goal_usd"] for r in rows] == pytest.approx([10.5, 10.5, 11, 11])
    for r, p in zip(rows, tape.mt.positions, strict=True):
        assert (
            p.sl
            == r["request"]["sl"]
            == pytest.approx(
                r["raw_chart_sl"]
                + (round(r["send_ask"] - r["send_bid"], 4) if side < 0 else 0)
            )
        )
        assert p.tp == r["request"]["tp"]


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("clock", ["tick", "by_route_close", "explicit_close"])
def test_tick_advances_inside_final_verifier_never_submit_its_input_quote(
    tmp_path, monkeypatch, side, clock
):
    from bot.hull.desk import Desk
    from bot.hull.feed import Stream

    offset = 0 if clock == "tick" else 180000
    tape = Tape(
        tmp_path,
        monkeypatch,
        [(100, OFF), (offset + 200, ON), (offset + 300, ON + 0.125)],
        side=side,
    )
    if offset:
        tape.hidden = [(179999, ON)]
    if clock == "explicit_close":
        tape.cfg["hull"]["entry_timing"] = "confirmed_cross_next_bar"
    stage = None
    old_entry, old_project = Desk._entry_quote, Stream.project

    def entry(self, name, window, what):
        nonlocal stage
        stage = what
        try:
            return old_entry(self, name, window, what)
        finally:
            stage = None

    def project(self, mt, name, q):
        raw = old_project(self, mt, name, q)
        if (
            stage == "final_send"
            and self.tf == 3
            and len(mt.positions) == 1
            and tape.i == 1
        ):
            tape.i += 1
        return raw

    monkeypatch.setattr(Desk, "_entry_quote", entry)
    monkeypatch.setattr(Stream, "project", project)
    checks = []
    old_check, old_send = tape.mt.order_check, tape.mt.order_send

    def check(req):
        checks.append(dict(req))
        return old_check(req)

    def send(req):
        tick = tape.tick(None)
        assert (
            req["price"] == (tick.ask if side > 0 else tick.bid) and req == checks[-1]
        )
        return old_send(req)

    tape.mt.order_check, tape.mt.order_send = check, send
    tape.run(count=2)
    assert len(tape.mt.positions) == 4 and len(checks) == 5
    assert tape.events("demo_result")[1]["unsent_reprices"] == 1
