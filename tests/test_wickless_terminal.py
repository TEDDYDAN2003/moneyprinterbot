"""Pinned MT5 test double: no exchange, account or network is used."""

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from hull_mt import MT
from wickless_qa.fixture import fixture

from bot.broker import DemoSafetyError
from bot.config import entry_sessions as es
from bot.hull.orders import Block, Grid
from bot.wickless import terminal
from bot.wickless.config import Config, Pair
from bot.wickless.engine import TF, Signal, Zone
from bot.wickless.execution import MAGIC, Intents, execute

B = 1704204000  # Tue 2 Jan 2024 14:00 UTC, inside the gold overlap


class Fake(MT):
    TIMEFRAME_M15 = 15

    def __init__(self):
        super().__init__()
        self.q.time_msc = B * 1000 + 50
        self.mtclosed = False

    def initialize(self, *a):
        return True

    def shutdown(self):
        self.mtclosed = True

    def order_send(self, r):
        x = super().order_send(r)
        if x and "position" not in r:
            x.order = x.deal
        return x


def conf(mode="demo"):
    return Config(
        version="wickless-m15/v1",
        broker="Test",
        server="t",
        mode=mode,
        pairs=[Pair(symbol="XAUUSDm", lots=0.01, max_spread_pips=3, max_risk_usd=25)],
    )


def signal(d=1):
    z = Zone(d, B - 2 * TF, 0, 1999900, 2000100, 1999000 if d == 1 else 2000800)
    return Signal(z, B, 2000000)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    m = Fake()
    cfg = conf()
    g = Grid.from_info(m.i)
    b = Intents(tmp_path / "state.db")

    def go(d=1, armed=True, now=None, config=None, stamp=B):
        c = config or cfg
        sig = signal(d)
        sig = replace(sig, close_s=stamp, zone=replace(sig.zone, born=stamp - 2 * TF))
        return execute(
            m,
            c,
            c.pairs[0],
            g,
            sig,
            (1, "t"),
            armed=armed,
            book=b,
            now=now or (lambda: stamp + 0.2),
        )

    yield m, b, go
    b.close()


@pytest.mark.parametrize("d", [1, -1])
@pytest.mark.parametrize("slip", [0, 0.03, -0.03])
def test_attached_stop_and_1r_from_actual_fill(rig, d, slip):
    m, b, go = rig
    m.retprice = round((m.q.ask if d == 1 else m.q.bid) + slip, 3)
    r = go(d)
    assert (
        r["status"] == "demo_fill_verified"
        and r["risk_usd"] > 0
        and b.unresolved() == []
    )
    assert r["tp"] - r["entry"] == pytest.approx(r["entry"] - r["sl"])
    assert len(m.requests) == (1 if not slip else 2)
    assert all(x["sl"] == r["sl"] and x["magic"] == MAGIC for x in m.requests)
    if slip:
        assert (
            m.requests[1]["action"] == m.TRADE_ACTION_SLTP
            and m.requests[1]["position"] == r["ticket"]
        )
    m.positions.clear()
    with pytest.raises(Block, match="already_consumed"):
        go(d)


def test_observer_never_sends_or_reserves_and_consumes_correct_quote(rig):
    m, b, go = rig
    for d in (1, -1):
        result = go(d, False, config=conf("paper"))
        assert result["status"] == "observer_plan"
        assert result["entry"] == (m.q.ask if d == 1 else m.q.bid)
    assert not m.requests and not b.db.execute("SELECT * FROM intents").fetchall()


@pytest.mark.parametrize(
    "edit,reason",
    [
        (lambda m: setattr(m.a, "trade_mode", 2), "DEMO"),
        (lambda m: setattr(m.a, "margin_mode", 0), "DEMO"),
        (lambda m: setattr(m.a, "server", "other"), "changed"),
        (lambda m: setattr(m.a, "trade_expert", False), "permission"),
        (lambda m: setattr(m, "orders", [NS()]), "exposure"),
        (lambda m: setattr(m, "positions", [NS(magic=99)]), "exposure"),
        (lambda m: setattr(m, "positions_get", lambda: None), "unavailable"),
        (lambda m: setattr(m.q, "time_msc", B * 1000 - 1), "waiting_post_close_quote"),
        (
            lambda m: setattr(m.q, "time_msc", B * 1000 + 500),
            "waiting_post_close_quote",
        ),
        (lambda m: setattr(m.q, "ask", 2001.0), "spread_cap"),
        (lambda m: setattr(m.q, "bid", 1999.88), "through_base"),
        (lambda m: setattr(m, "order_calc_profit", lambda *a: -50), "risk_cap"),
        (lambda m: setattr(m, "order_check", lambda r: None), "order_check_rejected"),
        (lambda m: setattr(m.i, "trade_mode", 3), "side_disabled"),
        (lambda m: setattr(m.i, "trade_stops_level", 5000), "spec_changed"),
        (lambda m: setattr(m.i, "filling_mode", 0), "filling_policy"),
        (lambda m: Path("KILL").touch(), "KILL"),
    ],
)
def test_broker_vetoes_send_nothing(rig, edit, reason):
    m, b, go = rig
    edit(m)
    with pytest.raises((Block, DemoSafetyError), match=reason):
        go()
    assert not m.requests and not b.unresolved()


def test_late_signal_or_slow_order_check_cannot_send(rig):
    m, _b, go = rig
    with pytest.raises(Block, match="outside_entry_clock"):
        go(now=lambda: B + 9)
    clock = iter((B + 0.2, B + 9))
    with pytest.raises(Block, match="send_window_closed"):
        go(now=lambda: next(clock))
    assert not m.requests


@pytest.mark.parametrize(
    "failure", ["unknown", "partial", "bad_ticket", "missing_sl", "amend"]
)
def test_uncertainty_halts_across_restart_without_resend(rig, tmp_path, failure):
    m, b, go = rig
    send = m.order_send

    def damaged(r):
        res = send(r)
        if failure == "unknown":
            return None
        if failure == "partial":
            res.volume = 0.001
        if failure == "bad_ticket":
            res.order = res.deal = 0
        if failure == "missing_sl":
            m.positions[0].sl = 0
        if failure == "amend" and "position" in r:
            return None
        return res

    m.order_send = damaged
    if failure == "amend":
        m.retprice = 2000.12
    with pytest.raises(DemoSafetyError, match="reconcile"):
        go()
    again = Intents(tmp_path / "state.db")
    assert again.unresolved()
    assert b.unresolved()
    with pytest.raises(DemoSafetyError, match="Unresolved"):
        again.reserve("OTHER", B + TF)
    assert len(m.requests) == (2 if failure == "amend" else 1)
    again.close()


def test_paper_arm_real_account_and_bad_server_refused(rig):
    m, _, go = rig
    with pytest.raises(DemoSafetyError):
        go(config=conf("paper"))
    with pytest.raises(DemoSafetyError, match="Wrong MT5 server"):
        go(config=conf().model_copy(update={"server": "Wrong"}))
    with pytest.raises(DemoSafetyError, match="private mode"):
        terminal.run(conf("paper"), "trade", arm=True)
    assert not m.requests


@pytest.mark.parametrize(
    "rejection,expected", [(B - 6 * 3600, "observer_plan"), (B + 8 * 3600, es.OUTSIDE)]
)
def test_stream_and_full_loop_with_closed_synthetic_history(
    monkeypatch, tmp_path, capsys, rejection, expected
):
    # Warm only the prefix; a subsequent, live rejection must reach the actual observer loop.
    from bot.wickless.engine import Bar, Engine

    m = Fake()
    cfg = conf("paper")
    p = cfg.pairs[0]
    bars = fixture()
    m.i.trade_tick_size = m.i.point = 0.01
    m.i.digits = 2
    p = p.model_copy(update={"pip_size": 0.1})
    cfg = cfg.model_copy(update={"pairs": [p]})
    eng = Engine(cfg.rules, 30)
    at = next(
        i
        for i, r in enumerate(bars)
        if eng.update(Bar(*r)).signal
        and i > 120
        and eng.last - r[0] == 0
        and eng.structure.i > 100
    )
    # Same exact price path: only change whether its rejection closes at OPEN or END.
    offset = rejection - (bars[at][0] + TF)
    bars = [[t + offset, *prices] for t, *prices in bars]
    start = at - 25
    # Use exactly the prefix as warm-up so the indicator has the same initial structure.
    monkeypatch.setattr(terminal, "HISTORY", start)
    samples = [
        {"time": t, "open": o / 100, "high": h / 100, "low": l / 100, "close": c / 100}
        for t, o, h, l, c in bars
    ]
    ptr = [start]
    calls = []

    def read(name, tf, pos, n):
        calls.append((name, tf, pos, n))
        return samples[ptr[0] - n : ptr[0]]

    m.copy_rates_from_pos = read

    def now():
        return samples[ptr[0] - 1]["time"] + TF + 0.2

    def sleep(_):
        ptr[0] += 1
        if ptr[0] > at + 6:
            raise KeyboardInterrupt

    def tick(_):
        c = samples[ptr[0] - 1]["close"]
        return NS(time_msc=int((now() - 0.1) * 1000), bid=c, ask=round(c + 0.01, 2))

    m.symbol_info_tick = tick
    monkeypatch.setitem(sys.modules, "MetaTrader5", m)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(terminal, "time", NS(time=now, sleep=sleep))
    with pytest.raises(KeyboardInterrupt):
        terminal.run(cfg, "observe")
    log = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    (target,) = [
        x for x in log if x["event"] == "entry_decision" and x["close_s"] == rejection
    ]
    assert target["status"] == expected and target["setup_open_s"] < rejection
    assert target["signal_session"]["market"] == "XAUUSD"
    assert any(
        x["event"] == "m15_close" and x["close_s"] == rejection and x["signal"]
        for x in log
    )
    assert target["signal_session"]["active"] == (
        ["london_before_new_york"] if expected == "observer_plan" else []
    )
    if expected == es.OUTSIDE:
        assert (
            target["session_clock"] == "rejection_close"
        )  # no waiting for the next open
    else:
        assert (
            not es.load_entry_sessions()
            .bind("XAUUSDm")
            .report(target["setup_open_s"])["active"]
        )
    assert (
        not m.requests
        and m.mtclosed
        and all(tf == 15 and pos == 1 for _, tf, pos, _ in calls)
    )
    assert (tmp_path / "reports/wickless_runtime").is_dir()
    # No stale submissions following history outage/rewarm.
    lane = terminal.Stream(m, p, cfg.rules)
    assert lane.refresh(now())[1] == "warmup_only"
    lane.wait = signal()
    m.copy_rates_from_pos = lambda *args: None
    assert (
        lane.refresh(now())[1].startswith("history_unavailable") and lane.wait is None
    )
    m.copy_rates_from_pos = read
    assert lane.refresh(now()) == (None, "warmup_only") and not lane.eng.pending


def test_quote_changed_during_broker_precheck_and_alternative_deal_link(rig):
    m, b, go = rig
    check = m.order_check

    def move(req):
        # Return a NEW quote object, like the MT5 bridge (not mutating the captured object).
        m.q = NS(time_msc=B * 1000 + 105, bid=2000.001, ask=2000.101)
        return check(req)

    m.order_check = move
    with pytest.raises(Block, match="quote_changed"):
        go()
    assert not m.requests and not b.unresolved()
    m.order_check = check
    send = m.order_send

    def alternate(req):
        res = send(req)
        res.order = 81234
        return res

    m.order_send = alternate

    def deals(*, position):
        return [NS(ticket=1, position_id=position, magic=MAGIC, symbol="XAUUSDm")]

    m.history_deals_get = deals
    assert go()["status"] == "demo_fill_verified"


def test_explicit_selection_and_foreign_account_lock(rig, tmp_path, monkeypatch):
    from contextlib import ExitStack

    from bot.broker import _account_order_lock

    m, b, _ = rig
    c = conf()
    foreign = c.pairs[0].model_copy(update={"symbol": "OTHER"})
    with pytest.raises(Block, match="not_selected"):
        execute(
            m,
            c,
            foreign,
            Grid.from_info(m.i),
            signal(),
            (1, "t"),
            armed=True,
            book=b,
            now=lambda: B + 0.2,
        )
    monkeypatch.chdir(tmp_path)
    with ExitStack() as stack:
        terminal.protected_lock(stack, tmp_path / "private", "account")
        with (
            pytest.raises(DemoSafetyError, match="lock"),
            _account_order_lock(tmp_path / "private/hull/account/scanner.lock", 0.1),
        ):
            pass
    assert not m.requests


def test_preflight_never_sends_and_unresolved_blocks_next_armed_start(
    monkeypatch, tmp_path, capsys
):
    import hashlib

    m = Fake()
    monkeypatch.setitem(sys.modules, "MetaTrader5", m)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(terminal.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(terminal, "HISTORY", 7)
    bar = lambda i: {
        "time": B - (7 - i) * TF,
        "open": 2000.0,
        "high": 2000.3,
        "low": 1999.4,
        "close": 1999.9,
    }
    m.copy_rates_from_pos = lambda name, frame, pos, n: [
        bar(i) for i in range(7 - n, 7)
    ]
    monkeypatch.setattr(terminal, "time", NS(time=lambda: B + 0.2))
    terminal.run(conf(), "preflight")
    events = [json.loads(row) for row in capsys.readouterr().out.splitlines()]
    assert (
        next(v for v in events if v["event"] == "startup")["entry_sessions"]["version"]
        == es.VERSION
    )
    assert next(v for v in events if v["event"] == "symbol_check")["entry_session"][
        "active"
    ] == ["london_ny_overlap", "new_york_day_session"]
    assert not m.requests and not (tmp_path / ".moneyprinterbot").exists()
    key = hashlib.sha256(repr((1, "t")).encode()).hexdigest()[:16]
    book = Intents(tmp_path / ".moneyprinterbot/wickless" / key / "intents.sqlite3")
    book.reserve("XAUUSDm", B)
    book.close()
    with pytest.raises(DemoSafetyError, match="Unresolved"):
        terminal.run(conf(), "trade", arm=True)
    assert not m.requests
    # Reconciliation never sends and is only allowed after human review while flat.
    m.orders = [NS()]
    with pytest.raises(DemoSafetyError, match="flat account"):
        terminal.run(conf(), "reconcile", review_flat=True)
    m.orders = []
    terminal.run(conf(), "reconcile", review_flat=True)
    book = Intents(tmp_path / ".moneyprinterbot/wickless" / key / "intents.sqlite3")
    assert not book.unresolved()
    book.close()
    assert not m.requests


@pytest.mark.parametrize("armed", [False, True])
@pytest.mark.parametrize(
    "name,hour,expected",
    [
        ("USDJPYm", 0, ""),
        ("USDJPYm", 7, es.OUTSIDE),
        ("USDJPYm", 8, ""),
        ("GBPJPYm", 8, ""),
        ("EURUSDm", 7, es.OUTSIDE),
        ("EURUSDm", 8, ""),
        ("DE30m", 8, ""),
        ("JP225m", 15, es.OUTSIDE),
        ("AUDUSDm", 15, ""),
        ("USTEC_x100m", 22, es.OUTSIDE),
        ("USTEC_x100m", 16, ""),
        ("US500m", 15, ""),
        ("XAUUSD.pro", 15, es.UNMAPPED),
        ("1HZ100V", 15, es.UNMAPPED),
    ],
)
def test_selected_broker_pair_uses_shared_exact_schedule(
    rig, armed, name, hour, expected
):
    m, book, go = rig
    stamp = B + (hour - 14) * 3600
    c = conf("demo" if armed else "paper")
    c = c.model_copy(update={"pairs": [c.pairs[0].model_copy(update={"symbol": name})]})
    m.i.name = name
    m.q.ask = (
        m.q.bid + m.i.trade_tick_size
    )  # valid one-tick spread on the shared fake grid
    m.q.time_msc = stamp * 1000 + 50
    if expected:
        with pytest.raises(es.EntrySessionVeto, match=expected) as e:
            go(config=c, armed=armed, stamp=stamp)
        assert e.value.fields["session_clock"] == "rejection_close"
        assert not book.db.execute("SELECT * FROM intents").fetchall()
    else:
        v = go(config=c, armed=armed, stamp=stamp)
        assert v["status"] == ("demo_fill_verified" if armed else "observer_plan")
        assert v["entry_session"]["active"]
        if name == "US500m":
            assert v["entry_session"]["active"] == [
                "overlap_killzone",
                "ny_cash_first_hour",
                "new_york_day_session",
            ]
    assert len(m.requests) == int(armed and not expected)


@pytest.mark.parametrize("stage", ["observer_plan", "post_precheck", "final_send"])
def test_time_edge_while_pricing_checking_or_reserving(rig, stage):
    m, book, go = rig
    end = B + 8 * 3600
    # Inject an off-grid signal 2s before close to distinguish this gate from
    # the independent 8s age limit. Real M15 bar times are grid-checked upstream.
    stamp = end - 2
    clock = [stamp + 0.2]
    m.q.time_msc = stamp * 1000 + 100
    if stage == "observer_plan":
        obj, method = m, "order_calc_profit"
    elif stage == "post_precheck":
        obj, method = m, "order_check"
    else:
        obj, method = book, "reserve"
    real = getattr(obj, method)

    def slow(*args):
        result = real(*args)
        clock[0] = end
        return result

    setattr(obj, method, slow)
    with pytest.raises(es.EntrySessionVeto, match=es.OUTSIDE) as e:
        go(stamp=stamp, armed=stage != "observer_plan", now=lambda: clock[0])
    assert (
        e.value.fields["stage"] == stage
        and e.value.fields["session_clock"] == "decision"
    )
    assert not m.requests and not book.unresolved()
    rows = book.db.execute("SELECT state,detail FROM intents").fetchall()
    assert rows == ([("not_sent", es.OUTSIDE)] if stage == "final_send" else [])
    if rows:
        with pytest.raises(Block, match="already_consumed"):
            book.reserve("XAUUSDm", stamp)


def test_fill_slippage_tp_correction_is_not_an_entry_after_hours(rig):
    m, book, go = rig
    end = B + 8 * 3600
    stamp = end - 2
    clock = [stamp + 0.2]
    m.q.time_msc = stamp * 1000 + 100
    m.retprice = 2000.12
    send = m.order_send

    def fill(req):
        res = send(req)
        if "position" not in req:
            clock[0] = end + 0.25
            m.q.time_msc = end * 1000 + 100
        return res

    m.order_send = fill
    result = go(stamp=stamp, now=lambda: clock[0])
    assert result["status"] == "demo_fill_verified" and not book.unresolved()
    assert len(m.requests) == 2 and m.requests[1]["action"] == m.TRADE_ACTION_SLTP
    assert (
        m.requests[1]["position"] == result["ticket"]
        and m.requests[0]["sl"] == m.requests[1]["sl"]
    )
    assert result["tp"] - result["entry"] == pytest.approx(
        result["entry"] - result["sl"]
    )


def test_invalid_shared_policy_cannot_send_or_reserve_even_with_good_quote(
    rig, monkeypatch, tmp_path
):
    m, book, go = rig
    f = tmp_path / "broken.yaml"
    f.write_text("wrong: true")
    monkeypatch.setattr(es, "POLICY_FILE", f)
    with pytest.raises(es.EntrySessionVeto, match=es.UNAVAILABLE):
        go()
    assert not m.requests and not book.db.execute("SELECT * FROM intents").fetchall()
