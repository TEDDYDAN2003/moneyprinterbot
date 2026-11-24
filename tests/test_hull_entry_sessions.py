"""Entry-only admission, actual desk/dispatch with fabricated quotes and broker receipts."""

import json
from dataclasses import replace

import pytest
from hull_mt import ROOT, T, sig

from bot.config import entry_sessions as es
from bot.config.loader import load_yaml
from bot.hull.curves import LiveTick
from bot.hull.entry_clock import WAITING
from bot.hull.hours import Hours
from bot.hull.structure import Structure
from bot.hull.terminal import dispatch

NAME = "XAUUSDm"
OPEN, END = T - 7 * 3600, T + 7 * 3600  # winter gold union, Tue 08:00–22:00 UTC
OK = {"data_ok": True, "history_ok": True, "rewarmed": False}


def test_all_shipped_hull_aliases_resolve_without_substring_guessing():
    policy = es.load_entry_sessions()
    cfg = load_yaml(ROOT / "config.yaml")["hull"]
    for key, p in cfg["symbols"].items():
        if key == "1HZ100V":
            assert policy.bind(key).issue == es.UNMAPPED
            continue
        for alias in (key, *(p.get("aliases", [])), p.get("broker_symbol") or key):
            g = policy.bind(alias, key)
            assert g.market == ("DE40" if key == "DE30" else key) and not g.issue


def wall_at(monkeypatch, stamp):
    clock = [stamp]
    monkeypatch.setattr("bot.hull.desk.time.time", lambda: clock[0])
    return clock


def signal_at(stamp, tf=15, side=1):
    return replace(
        sig(sl=1999 if side == 1 else 2002, side=side),
        tf=tf,
        close_s=stamp,
        structure=Structure(closed_s=stamp, bias=side),
    )


@pytest.mark.parametrize("armed", [False, True])
@pytest.mark.parametrize(
    "name,key,hour,expected",
    [
        ("USDJPYm", "USDJPY", 0, ""),
        ("USDJPYm", "USDJPY", 7, es.OUTSIDE),
        ("USDJPYm", "USDJPY", 8, ""),
        ("JP225m", "JP225", 0, ""),
        ("JP225m", "JP225", 15, es.OUTSIDE),
        ("DE30m", "DE40", 8, ""),
        ("DE30m", "DE30", 9, es.OUTSIDE),
        ("GBPJPYm", "GBPJPY", 10, ""),
        ("AUDUSDm", "AUDUSD", 0, es.OUTSIDE),
        ("AUDUSDm", "AUDUSD", 16, ""),
        ("USTEC_x100m", "NAS100USD", 16, ""),
        ("USTEC_x100m", "NAS100USD", 22, es.OUTSIDE),
        ("US30m", "US30", 15, ""),
        ("US500m", "US500", 16, ""),
        ("US500m", "US500", 22, es.OUTSIDE),
        ("1HZ100V", "1HZ100V", 15, es.UNMAPPED),
        ("XAUUSD.r", "XAUUSD", 15, es.UNMAPPED),
        ("EURUSDm", "XAUUSD", 15, es.MISMATCH),
    ],
)
def test_observer_and_armed_share_exact_market_admission(
    hull_desk, monkeypatch, armed, name, key, hour, expected
):
    mt, d, log = hull_desk("medium", armed=armed)
    # One deterministic synthetic price/volume grid across instruments. Even a
    # legacy continuous profile must not bypass the mandatory policy.
    d.syms = {name: (key, d.syms[NAME][1].model_copy(update={"session": "continuous"}))}
    d.h = d.h.model_copy(update={"eligible_timeframes": ("M15",)})
    stamp = T + (hour - 15) * 3600
    mt.i.name = name
    mt.q.time_msc = stamp * 1000 + 100
    wall_at(monkeypatch, stamp + 0.2)
    r = signal_at(stamp)
    dispatch(d, name, [r], **OK)
    (decision,) = [v for e, v in log if e == "label_decision"]
    assert decision["outcome"] == (
        expected or ("demo_batch_sent" if armed else "shadow_plan_only")
    )
    assert decision["signal_session"]["status"] == (expected or "in_symbol_session")
    assert len(mt.requests) == (4 if armed and not expected else 0)
    if expected:
        (veto,) = [v for e, v in log if e == "entry_session_veto"]
        assert (
            veto["profile"] == key
            and veto["reason"] == expected
            and veto["session_clock"] == "signal"
        )
        assert not d.waiting and d.batch(name, r) == "already_consumed"
    else:
        (preview,) = [v for e, v in log if e == "batch_preview"]
        assert preview["entry_session"]["active"]


@pytest.mark.parametrize("armed", [False, True])
@pytest.mark.parametrize("direction", ["into", "out_of"])
def test_live_trigger_not_future_projected_bar_end_or_newly_open_submission(
    hull_desk, monkeypatch, armed, direction
):
    mt, d, _ = hull_desk("medium", armed=armed)
    d.h = d.h.model_copy(update={"eligible_timeframes": ("M45",)})
    edge = OPEN if direction == "into" else END
    at = edge - 0.25
    # Forming M45 extends past the edge; a quote a fraction later can be in
    # a different session even though the raw signal is still fresh.
    mt.q.time_msc = int(at * 1000)
    r = replace(signal_at(edge + 1800, 45), trigger=LiveTick.read(mt.q))
    d.verify_signal = lambda *_: True
    now = edge + 0.2 if direction == "into" else edge - 0.1
    wall_at(monkeypatch, now)
    mt.q.time_msc = int(now * 1000)
    assert d.batch(NAME, r) == "await_cross_candle_close" and not mt.requests
    # A *confirmed* cross just outside the window does not become valid on a
    # post-close quote inside it; a confirmed close exactly at the open qualifies.
    wall_at(monkeypatch, edge + 0.2)
    mt.q.time_msc = edge * 1000 + 100
    confirmed = signal_at(edge, 45)
    why = d.batch(NAME, confirmed)
    assert why == (
        es.OUTSIDE
        if direction == "out_of"
        else "demo_batch_sent"
        if armed
        else "shadow_plan_only"
    )
    assert len(mt.requests) == (4 if armed and direction == "into" else 0)


def test_first_quote_wait_cannot_resume_across_end_or_catch_up_tomorrow(
    hull_desk, monkeypatch
):
    mt, d, log = hull_desk()
    d.h = d.h.model_copy(update={"eligible_timeframes": ("M15",)})
    d.hours = Hours({})
    r = signal_at(END - 900)
    clock = wall_at(monkeypatch, r.signal_s + 0.2)
    q, mt.q = mt.q, None
    assert d.batch(NAME, r) == WAITING
    clock[0] = END
    mt.q = q
    mt.q.time_msc = END * 1000
    assert (
        d.resume_first_quote(NAME, 15) == es.OUTSIDE
        and not d.waiting
        and not mt.requests
    )
    clock[0] = OPEN + 86400
    assert d.batch(NAME, r) == "already_consumed"
    assert not any(e == "batch_preview" for e, _ in log)


@pytest.mark.parametrize("stage", ["pre_send", "final_send", "next_leg"])
def test_edge_during_broker_precheck_write_ahead_or_partial_batch(
    hull_desk, monkeypatch, stage
):
    mt, d, log = hull_desk("medium")
    d.h = d.h.model_copy(update={"eligible_timeframes": ("M45",)})
    # Isolate the mandatory market closing edge from the separate local rollover blackout.
    d.hours = Hours({})
    mt.q.time_msc = END * 1000 - 1750
    r = signal_at(END - 2, 45)  # synthetic close within the clock-edge test window
    d.verify_signal = lambda *_: True
    clock = wall_at(monkeypatch, END - 1.5)

    def advance():
        clock[0] = END
        mt.q.time_msc = END * 1000

    if stage == "pre_send":
        check = mt.order_check

        def slow(req):
            result = check(req)
            advance()
            return result

        mt.order_check = slow
    elif stage == "final_send":
        persist = d.persist

        def slow():
            persist()
            if d.state.get("manual_hold"):
                advance()

        d.persist = slow
    else:
        send = mt.order_send

        def slow(req):
            result = send(req)
            advance()
            return result

        mt.order_send = slow
    already = int(stage == "next_leg")
    assert (
        d.batch(NAME, r)
        == ("protected_partial_no_topup:" if already else "") + es.OUTSIDE
    )
    assert len(mt.requests) == len(mt.positions) == len(d.ladders.book) == already
    assert all(p.sl > 0 and p.tp > p.price_open for p in mt.positions)
    assert not d.state.get("manual_hold") and not d.waiting
    (veto,) = [v for e, v in log if e == "entry_session_veto"]
    assert (veto["stage"], veto["session_clock"], veto["legs_already_sent"]) == (
        stage,
        "decision",
        already,
    )
    assert d.batch(NAME, r) == "already_consumed"


def test_global_blackout_remains_an_additional_veto(hull_desk):
    mt, d, _ = hull_desk()
    d.hours = Hours(
        {
            "sessions": {
                "rollover_blackout": {"tz": "UTC", "start": "14:00", "end": "16:00"}
            }
        }
    )
    assert not d.session_gate(NAME).veto(T)
    assert d.batch(NAME, sig()) == "rollover_blackout" and not mt.requests


@pytest.mark.parametrize("protection", ["opposite", "mss", "kill"])
@pytest.mark.parametrize("missing_policy", [False, True])
def test_existing_ticket_protection_after_close_and_even_if_policy_lost(
    hull_desk, monkeypatch, tmp_path, protection, missing_policy
):
    mt, d, _ = hull_desk("medium")
    assert d.batch(NAME, sig()) == "demo_batch_sent"
    ids = {p.ticket for p in mt.positions}
    if missing_policy:
        monkeypatch.setattr(es, "POLICY_FILE", tmp_path / "missing.yaml")
        _, d, _ = hull_desk("medium", state=json.loads(d.path.read_text()), mt=mt)
    wall_at(monkeypatch, END + 0.25)
    mt.q.time_msc = END * 1000 + 100
    assert d.batch(NAME, signal_at(END, 2)) == (
        es.UNAVAILABLE if missing_policy else es.OUTSIDE
    )
    mt.requests.clear()
    d.supervise()
    assert (
        not mt.requests and {p.ticket for p in mt.positions} == ids
    )  # no forced session exit
    mt.q.bid = mt.positions[0].price_open + 0.80
    mt.q.ask = mt.q.bid + 0.1
    mt.q.time_msc += 1
    d.supervise()
    assert {
        r["position"] for r in mt.requests if r["action"] == mt.TRADE_ACTION_SLTP
    } == ids
    mt.requests.clear()
    if protection == "kill":
        d.kill.touch()
        d.supervise()
    elif protection == "opposite":
        dispatch(d, NAME, [signal_at(END, 1, -1)], **OK)
    else:
        shift = replace(
            signal_at(END, 1, -1), structure=Structure(END, -1, -2), label=0, side=0
        )
        dispatch(d, NAME, [], **OK, shifts=[shift])
    assert not mt.positions and len(mt.requests) == len(ids)
    assert {r["position"] for r in mt.requests} == ids


def test_opposite_seen_between_legs_at_session_end_still_closes_filled_leg(
    hull_desk, monkeypatch
):
    mt, d, log = hull_desk("medium")
    d.h = d.h.model_copy(update={"eligible_timeframes": ("M45",)})
    # Isolate the mandatory market closing edge from the separate local rollover blackout.
    d.hours = Hours({})
    mt.q.time_msc = END * 1000 - 1750
    r = signal_at(END - 2, 45)  # synthetic close within the clock-edge test window
    clock = wall_at(monkeypatch, END - 1.5)
    send = mt.order_send

    def fill(req):
        receipt = send(req)
        if "position" not in req:
            clock[0] = END + 0.25
            mt.q.time_msc = END * 1000 + 100
        return receipt

    mt.order_send = fill

    def verify(name, reading, q):
        if q.time_msc >= END * 1000:
            opposite = replace(
                signal_at(reading.close_s + 2700, 45, -1), trigger=LiveTick.read(q)
            )
            assert d.close_opposite(name, opposite) == 1
            return False
        return True

    d.verify_signal = verify
    assert d.batch(NAME, r) == "protected_partial_no_topup:" + es.OUTSIDE
    assert (
        not mt.positions and len(mt.requests) == 2 and mt.requests[1]["position"] == 1
    )
    assert not d.state.get("manual_hold") and not d.waiting
    assert any(
        e == "entry_session_veto" and v["legs_already_sent"] == 1 for e, v in log
    )
