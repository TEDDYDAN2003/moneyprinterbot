"""Fabricated MT5 hedging broker: acceptance, fills, cancellations and history lag are independent."""

import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_mt import MT, ROOT, T, scanner_clock
from test_hull_paired_execution import tape

from bot.broker import DemoSafetyError
from bot.config.loader import load_yaml
from bot.hull.bar import Bar
from bot.hull.config import RETEST, HullConfig, selection
from bot.hull.desk import Desk
from bot.hull.orders import Block, cross_candle_stop
from bot.hull.retest import capability, level
from bot.hull.signal import Reading
from bot.hull.structure import Structure

NAME = "XAUUSDm"


class PendingMT(MT):
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_REMOVE = 8
    ORDER_TIME_SPECIFIED = 2
    ORDER_FILLING_RETURN = 2
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3

    def __init__(self):
        super().__init__()
        self.i.order_mode = 55
        self.i.expiration_mode = 4
        self.i.trade_tick_size = 0.01
        self.now = T + 0.1
        self.archive = {}
        self.n = 999
        self.checks = []
        self.margins = []
        self.after_send = lambda req: None

    def tick(self, bid=2000, spread=0.06):
        self.q = NS(time_msc=int(self.now * 1000), bid=bid, ask=bid + spread)

    def order_calc_margin(self, *a):
        self.margins.append(a)
        return super().order_calc_margin(*a)

    def order_check(self, r):
        self.checks.append(dict(r))
        return super().order_check(r)

    def history_orders_get(self, *, ticket):
        return [self.archive[ticket]] if ticket in self.archive else []

    def history_deals_get(self, *args, ticket=None, position=None):
        return [
            d
            for d in self.deals
            if (ticket is None or d.order == ticket)
            and (position is None or d.position_id == position)
        ]

    def order_send(self, r):
        if r["action"] == self.TRADE_ACTION_PENDING:
            assert (
                r["type"] in (2, 3)
                and r["sl"] > 0
                and r["tp"] > 0
                and r["expiration"] > self.now
                and r["type_time"] == 2
            )
            self.requests.append(dict(r))
            self.n += 1
            t = self.n
            self.orders.append(
                NS(
                    ticket=t,
                    magic=r["magic"],
                    symbol=r["symbol"],
                    type=r["type"],
                    comment=r["comment"],
                    price_open=r["price"],
                    sl=r["sl"],
                    tp=r["tp"],
                    time_expiration=r["expiration"],
                    type_time=r["type_time"],
                    volume_initial=r["volume"],
                    volume_current=r["volume"],
                    time_setup_msc=int(self.now * 1000),
                    position_id=0,
                    state=1,
                )
            )
            self.after_send(r)
            return NS(retcode=10008, order=t, deal=0)
        if r["action"] == self.TRADE_ACTION_REMOVE:
            self.requests.append(dict(r))
            x = next(x for x in self.orders if x.ticket == r["order"])
            self.terminate(x)
            return NS(retcode=10009)
        assert "position" in r, "no market fallback or duplicate entry"
        p = self.positions_get(ticket=r["position"])[0]
        res = super().order_send(r)
        if r["action"] == 1 and getattr(res, "retcode", None) == 10009:
            self.deals.append(
                NS(
                    ticket=600000 + len(self.deals),
                    order=500000 + len(self.deals),
                    position_id=p.identifier,
                    entry=1,
                    magic=r["magic"],
                    symbol=p.symbol,
                    type=r["type"],
                    time_msc=self.q.time_msc,
                    price=r["price"],
                    volume=r["volume"],
                )
            )
        return res

    def terminate(self, x, state=2):
        self.orders.remove(x)
        x.state = state
        self.archive[x.ticket] = x

    def fill(self, x, improve=0, qty=None, flat=False):
        qty = x.volume_initial if qty is None else qty
        side = 1 if x.type == 2 else -1
        px = round(x.price_open - side * improve, 3)
        p = NS(
            ticket=x.ticket,
            identifier=x.ticket,
            volume=qty,
            price_open=px,
            magic=x.magic,
            symbol=x.symbol,
            type=0 if side == 1 else 1,
            time_msc=int(self.now * 1000),
            sl=x.sl,
            tp=x.tp,
            profit=0,
        )
        self.positions.append(p)
        x.volume_current = round(x.volume_current - qty, 8)
        x.position_id = p.identifier
        z = NS(
            ticket=9000 + x.ticket,
            order=x.ticket,
            position_id=p.identifier,
            entry=0,
            magic=x.magic,
            type=p.type,
            symbol=x.symbol,
            time_msc=p.time_msc,
            price=px,
            volume=qty,
        )
        self.deals.append(z)
        if x.volume_current <= 0:
            self.terminate(x, 4)
        if flat:
            self.positions.remove(p)
            self.deals.append(
                NS(
                    **(
                        vars(z)
                        | {
                            "ticket": z.ticket + 20000,
                            "order": x.ticket + 30000,
                            "entry": 1,
                            "type": 1 - z.type,
                            "time_msc": p.time_msc + 10,
                        }
                    )
                )
            )
        return p


def make(tmp, mp, side=1, armed=True, hook=None):
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["run"]["mode"] = "demo"
    cfg["hull"]["entry_route"] = RETEST
    if hook:
        hook(cfg)
    h = HullConfig.model_validate(cfg["hull"])
    mt = PendingMT()
    mt.tick()
    scanner_clock(mp, lambda: mt.now, lambda _: None)
    logs = []
    syms = {NAME: ("XAUUSD", selection(h)["XAUUSD"])}
    path = tmp / "state.json"

    def desk(state):
        d = Desk(
            mt,
            cfg,
            h,
            syms,
            (1, "t"),
            state,
            path,
            lambda event, **kw: logs.append({"event": event, **kw}),
            armed,
        )
        d.indicator_key = "test"
        d.verify_signal = lambda *_: True
        return d

    d = desk({})
    child = 3
    b = Bar(T - 180, 2000 - side * 1.2, 2002, 1998, 2000)
    wick = b.l if side == 1 else b.h
    raw = Reading(
        T,
        5,
        b.c,
        1,
        2,
        1.2,
        side,
        side,
        "ready",
        wick,
        b.t,
        cross_candle_stop(wick, side, 0.12, 0.01),
        b.t,
        0,
        None,
        None,
        Structure(closed_s=T, bias=side),
        "low" if side == 1 else "high",
        stop_tick_size=0.01,
        trigger_tf=child,
        detected_s=mt.now,
        trigger_structure=Structure(closed_s=T, bias=side),
        cross_bar=b,
    )
    return mt, d, raw, logs, desk


@pytest.mark.parametrize(
    "sgn,o,h,lo,entry,seg",
    [
        (1, 99, 102, 98, 99.8, "body"),
        (-1, 101, 102, 98, 100.2, "body"),
        (1, 101, 102, 99.5, 99.9, "lower_wick"),
        (-1, 99, 100.5, 98, 100.1, "upper_wick"),
        (1, 100, 105, 99.5, 99.9, "lower_wick"),
        (-1, 100, 100.5, 95, 100.1, "upper_wick"),
    ],
)
def test_direction_geometry(sgn, o, h, lo, entry, seg):
    assert level(Bar(T, o, h, lo, 100), sgn, 10, 0.01, 0.2, 0.1) == (entry, seg)


def test_cap_min_and_off_grid():
    assert level(Bar(T, 112, 113, 98, 100), -1, 1, 0.025, 0.2, 0.1)[0] == 100.1
    for a in [Bar(T, 100, 105, 100, 100), Bar(T, 100, 105, 99.99, 100)]:
        with pytest.raises(Block):
            level(a, 1, 1, 0.025, 0.2, 0.1)
    v, _ = level(Bar(T, 99, 102, 98, 100.017), 1, 1, 0.025, 0.2, 0.1)
    assert 0.025 <= 100.017 - v <= 0.1 and round(v / 0.025) == pytest.approx(v / 0.025)


@pytest.mark.parametrize("side", [1, -1])
def test_ten_ack_before_any_fill_then_ten_owned_legs(tmp_path, monkeypatch, side):
    mt, d, r, logs, _ = make(tmp_path, monkeypatch, side)
    assert d.batch(NAME, r) == "retest_batch_placed"
    assert (
        len(mt.orders) == len(mt.requests) == 10
        and not mt.positions
        and not d.ladders.book
    )
    assert (
        len({o.price_open for o in mt.orders}) == 1
        and mt.orders[0].price_open == 2000 - side * 0.12
    )
    assert all(o.time_expiration == T + 180 and o.sl == r.raw_sl for o in mt.orders)
    assert d.batch(NAME, r) == "retest_batch_already_reserved"
    assert not [e for e in logs if e["event"] == "retest_fill_receipt"]
    mt.now += 24
    mt.tick(2000 - side * 0.25)
    [mt.fill(x) for x in list(mt.orders)]
    d.supervise()
    assert not d.retest.active and len(d.ladders.book) == len(mt.positions) == 10
    assert all(
        d.owners.lane(p) == 5 and d.owners.origin(p)["trigger_tf"] == 3
        for p in mt.positions
    )
    assert all(
        v["target_policy"] == "base_plus_placement_spread_v1"
        for v in d.ladders.book.values()
    )
    assert len([e for e in logs if e["event"] == "retest_fill_receipt"]) == 10
    assert d.batch(NAME, r) == "already_consumed"


def test_expiry_without_chase_or_new_ticket(tmp_path, monkeypatch):
    mt, d, r, *_ = make(tmp_path, monkeypatch)
    d.batch(NAME, r)
    mt.now = T + 180
    mt.tick()
    d.supervise()
    d.supervise()
    assert not mt.positions and not mt.orders and not d.retest.active
    assert len(mt.requests) == 20


@pytest.mark.parametrize(
    "kind",
    [
        "foreign",
        "expired",
        "too_close",
        "capability",
        "stale",
        "absent_bar",
        "wrong_anchor",
        "unverified",
        "margin",
        "kill",
        "spread",
    ],
)
def test_rejection_before_submission(tmp_path, monkeypatch, kind):
    mt, d, r, *_ = make(tmp_path, monkeypatch)
    if kind == "foreign":
        mt.orders.append(NS(ticket=713))
    if kind == "expired":
        mt.now += 8.5
        mt.tick()
    if kind == "too_close":
        mt.tick(1999.80)
    if kind == "capability":
        mt.i.expiration_mode = 1
    if kind == "stale":
        mt.q.time_msc = T * 1000 - 200
    if kind == "absent_bar":
        r = replace(r, cross_bar=None)
    if kind == "wrong_anchor":
        r = replace(r, anchor=r.anchor + 0.01)
    if kind == "unverified":
        d.verify_signal = lambda *_: False
    if kind == "margin":
        mt.a.margin_free = 10
    if kind == "kill":
        d.kill = tmp_path / "KILL"
        d.kill.touch()
    if kind == "spread":
        mt.tick(2000, 0.35)
    outcome = d.batch(NAME, r)
    assert outcome != "retest_batch_placed" and not mt.requests


def test_deadline_shared_and_no_timeout_replay(tmp_path, monkeypatch):
    mt, d, r, *_ = make(tmp_path, monkeypatch)

    def slow(_):
        mt.now += 1.01
        mt.tick()

    mt.after_send = slow
    out = d.batch(NAME, r)
    assert out == "entry_window_expired"
    n = len(mt.orders)
    assert n == 8
    d.supervise()
    assert len(mt.orders) == n
    mt.now = T + 181
    mt.tick()
    d.supervise()
    d.supervise()
    assert not mt.orders and d.batch(NAME, r) == "already_consumed"
    assert sum(z["action"] == 5 for z in mt.requests) == n


def test_read_only_ask_touch_not_bid_and_never_journal_fill(tmp_path, monkeypatch):
    mt, d, r, logs, _ = make(tmp_path, monkeypatch, armed=False)
    assert d.batch(NAME, r) == "shadow_retest_working"
    mt.now += 1
    mt.tick(1999.87)
    d.supervise()
    assert d.retest.active
    mt.now += 1
    mt.tick(1999.80)
    d.supervise()
    assert not d.retest.active and not mt.requests
    assert any(e.get("outcome") == "observed_quote_touch_not_fill" for e in logs)


def test_own_child_cancellation_and_fill_cancel_race(tmp_path, monkeypatch):
    mt, d, r, logs, _ = make(tmp_path, monkeypatch)
    d.batch(NAME, r)
    d.retest.interrupt(NAME, 10, T * 1000 + 200, -1, 5)
    assert not d.retest.active[NAME].get("cancel")
    d.retest.interrupt(NAME, 5, T * 1000 + 200, -1, 3)
    mt.now += 1
    mt.tick(1999.8)
    mt.fill(mt.orders[0])
    d.supervise()
    d.supervise()
    assert not mt.orders and not mt.positions and len(mt.requests) == 20
    assert any(e.get("event") == "exit_request" for e in logs)


def test_unknown_ack_never_adopt_by_comment_or_retry(tmp_path, monkeypatch):
    mt, d, r, _logs, reload = make(tmp_path, monkeypatch)
    orig = mt.order_send

    def lost(req):
        orig(req)
        return None

    mt.order_send = lost
    assert "unknown" in d.batch(NAME, r)
    assert len(mt.orders) == 1
    d2 = reload(json.loads(d.path.read_text()))
    d2.retest.recover((5,))
    d2.supervise()
    assert d2.batch(NAME, r) == "retest_reconciliation_required"
    assert len(mt.requests) == 1 and mt.orders


def test_restart_known_ids_cancels_never_places_remainder(tmp_path, monkeypatch):
    mt, d, r, _, reload = make(tmp_path, monkeypatch)
    d.batch(NAME, r)
    mt.now += 2
    mt.tick()
    mt.fill(mt.orders[-1])
    d2 = reload(json.loads(d.path.read_text()))
    with pytest.raises(DemoSafetyError):
        d2.retest.recover([10])
    d2.retest.recover([5])
    d2.supervise()
    d2.supervise()
    assert not mt.orders and not mt.positions and not d2.retest.active


def test_partial_fill_residual_cancel_protected_exit_not_a_topup(tmp_path, monkeypatch):
    mt, d, r, _, _ = make(tmp_path, monkeypatch)
    d.batch(NAME, r)
    mt.now += 1
    mt.tick(1999.8)
    mt.fill(mt.orders[0], qty=0.04)
    d.supervise()
    d.supervise()
    assert not mt.orders and not mt.positions
    assert len([q for q in mt.requests if q["action"] == 5]) == 10
    assert not d.retest.active


def test_filled_and_closed_before_poll_without_fabricated_open(tmp_path, monkeypatch):
    mt, d, r, logs, _ = make(tmp_path, monkeypatch)
    d.batch(NAME, r)
    mt.now += 2
    mt.tick()
    mt.fill(mt.orders[0], flat=True)
    d.supervise()
    assert not mt.positions and not d.ladders.book
    assert sum(e["event"] == "retest_fill_receipt" for e in logs) == 1


def test_history_lag_and_wrong_order_do_not_enroll(tmp_path, monkeypatch):
    mt, d, r, _, _ = make(tmp_path, monkeypatch)
    d.batch(NAME, r)
    mt.now += 1
    mt.tick()
    mt.fill(mt.orders[0])
    ds = mt.deals
    mt.deals = []
    d.supervise()
    assert not d.ladders.book and d.retest.active
    mt.deals = ds
    mt.deals[0].magic += 1
    d.supervise()
    assert (
        not d.ladders.book
        and d.retest.blocked(NAME) == "retest_reconciliation_required"
    )


def test_capability_masks_never_assumed():
    mt = PendingMT()
    assert not capability(mt, mt.i)
    mt.i.order_mode = 2
    assert capability(mt, mt.i)
    del mt.i.expiration_mode
    mt.i.order_mode = 55
    assert capability(mt, mt.i)


@pytest.mark.parametrize("side", [-1, 1])
def test_native_scanner_selects_pair_in_paper_without_any_sends(
    tmp_path, monkeypatch, side
):
    mt, rows, _, _ = tape(
        tmp_path,
        monkeypatch,
        broker=PendingMT(),
        route=RETEST,
        signs={tf: side for tf in (3, 5, 10, 15)},
        armed=False,
    )
    plans = [e for e in rows if e["event"] == "retest_batch_plan"]
    assert len(plans) == 1 and not mt.requests
    assert (plans[0]["tf"], plans[0]["trigger_tf"]) == (15, 10)
    assert plans[0]["side"] == side and plans[0]["legs"] == 4


def test_native_scanner_demo_places_one_batch_then_cancels_on_shutdown(
    tmp_path, monkeypatch
):
    mt, rows, _, _ = tape(tmp_path, monkeypatch, broker=PendingMT(), route=RETEST)
    assert [e["acknowledged"] for e in rows if e["event"] == "retest_dispatch"] == [4]
    assert len([x for x in mt.requests if x["action"] == 5]) == 4
    assert not mt.orders and not mt.positions


@pytest.mark.parametrize(
    "case",
    [
        "ok",
        "mismatched_deal",
        "missing_settlement",
        "forged_limit",
        "forged_close",
        "stripped_comment",
    ],
)
def test_real_book_receipt_to_standalone_journal(tmp_path, monkeypatch, case):
    from contextlib import closing

    monkeypatch.syspath_prepend(str(ROOT / "HullJournal"))
    monkeypatch.syspath_prepend(str(ROOT / "HullJournal/tests"))
    from fixtures import FakeMT, deal
    from ledger import MAGIC, db_open, identity, make_views, sync

    mt, d, r, logs, _ = make(tmp_path, monkeypatch)
    d.batch(NAME, r)
    mt.now += 40
    mt.tick()
    x = mt.orders[0]
    mt.fill(x, flat=True)
    [mt.terminate(z, 6) for z in list(mt.orders)]
    d.supervise()
    receipts = [e for e in logs if e["event"] == "retest_fill_receipt"]
    assert len(receipts) == 1
    e = receipts[0]
    di = deal(
        e["deal"],
        x.position_id,
        T + 40,
        0,
        0,
        x.volume_initial,
        x.price_open,
        comment="rewritten" if case == "stripped_comment" else x.comment,
        ms=100,
    )
    di.order = x.ticket + (1 if case == "mismatched_deal" else 0)
    do = deal(
        e["deal"] + 21000,
        x.position_id,
        T + 45,
        1,
        1,
        x.volume_initial,
        x.tp,
        profit=10,
        magic=0,
    )
    broker = FakeMT([di, do], T + 190)
    broker.login = 1
    broker.host = "t"
    if case == "forged_limit":
        e["request"]["price"] = 2001
    if case == "forged_close":
        e["trigger_bar"]["c"] = 1950
    if case == "missing_settlement":
        logs = [z for z in logs if z["event"] != "retest_batch_settled"]
    where = tmp_path / "reports/hull_runtime" / identity(broker, MAGIC)["key"]
    where.mkdir(parents=True)
    (where / "demo.jsonl").write_text("".join(json.dumps(x) + "\n" for x in logs))
    with closing(db_open(tmp_path / "ledger.sqlite3")) as db:
        sync(db, broker, broker.at, repo=tmp_path)
        out = make_views(db)[0]
        assert len(out["tickets"]) == 1 and out["tickets"][0]["net_profit"] == "10.00"
        assert len(out["setups"]) == int(case in ("ok", "stripped_comment"))
        if case in ("ok", "stripped_comment", "missing_settlement"):
            t = out["tickets"][0]
            assert (
                t["entry_timing"] == "confirmed_pair_retest_limit"
                and t["setup_type"] == RETEST
                and t["trigger_timeframe"] == "M3"
                and t["frozen_cross_sl"]
            )


@pytest.mark.parametrize(
    "case",
    [
        "account_pending_cap",
        "volume_limit",
        "changed_margin",
        "changed_quote",
        "placement_exception",
        "manual_tamper",
        "preflight_mask",
    ],
)
def test_mutable_safety_not_cached_for_speed(tmp_path, monkeypatch, case):
    mt, d, r, _logs, _ = make(tmp_path, monkeypatch)
    if case == "account_pending_cap":
        mt.a.limit_orders = 9
    elif case == "volume_limit":
        mt.i.volume_limit = 0.99
    elif case in ("changed_margin", "changed_quote"):
        orig = mt.order_check

        def check(q):
            if case == "changed_margin":
                mt.a.margin_free = 5
            else:
                mt.tick(2000.05)
            return orig(q)

        mt.order_check = check
    elif case == "placement_exception":

        def boom(_):
            raise TimeoutError("simulated disconnection")

        mt.order_send = boom
    elif case == "preflight_mask":
        mt.i.order_mode = 1
    if case != "manual_tamper":
        assert d.batch(NAME, r) != "retest_batch_placed" and not mt.orders
        if case == "placement_exception":
            assert d.retest.blocked(NAME) == "retest_reconciliation_required"
    else:
        d.batch(NAME, r)
        mt.orders[0].comment = "another order"
        d.retest.shutdown()
        assert len(mt.orders) == 1 and mt.orders[0].comment == "another order"
        assert d.retest.blocked(NAME) == "retest_reconciliation_required"


def test_server_expiry_clipped_before_local_maintenance(tmp_path, monkeypatch):
    def change(cfg):
        cfg["sessions"]["maintenance_start"] = "17:01"
        cfg["sessions"]["maintenance_end"] = "17:02"

    mt, d, r, _, _ = make(tmp_path, monkeypatch, hook=change)
    d.batch(NAME, r)
    assert {o.time_expiration for o in mt.orders} == {T + 60}


def test_minimum_funded_batch_not_double_counted(tmp_path, monkeypatch):
    mt, d, r, *_ = make(tmp_path, monkeypatch)
    mt.a.margin_free = 800
    assert d.batch(NAME, r) == "retest_batch_placed" and len(mt.orders) == 10


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("policy", ["fixed_tp", "profit_ladder_70_30_v1"])
def test_cash_goal_uses_placement_spread_and_real_better_fill(
    tmp_path, monkeypatch, side, policy
):
    mt, d, r, logs, _ = make(
        tmp_path, monkeypatch, side, hook=lambda c: c["hull"].update(exit_policy=policy)
    )
    d.batch(NAME, r)
    order = mt.orders[0]
    old_tp = order.tp
    mt.now += 5
    mt.tick(2000 - side * 0.24, spread=0.10)
    p = mt.fill(order, improve=0.05)
    d.supervise()
    assert p.sl == r.raw_sl and p.tp == pytest.approx(old_tp - side * 0.05)
    assert mt.order_calc_profit(
        p.type, NAME, p.volume, p.price_open, p.tp
    ) == pytest.approx(10.60)
    req = [q for q in mt.requests if q["action"] == 6]
    assert len(req) == 1 and req[0]["position"] == p.ticket
    assert len([e for e in logs if e["event"] == "retest_fill_receipt"]) == 1
    if policy == "fixed_tp":
        assert (
            d.state["target_by_ticket"][str(p.ticket)] == pytest.approx(10.60)
            and not d.ladders.book
        )
    else:
        assert d.ladders.book[str(p.ticket)]["entry"] == p.price_open


@pytest.mark.parametrize("case", ["becomes_legal", "touch_first", "late", "restart"])
def test_only_untouched_unsubmitted_limit_can_wait_for_legal_gap(
    tmp_path, monkeypatch, case
):
    from bot.hull.entry_clock import WAITING

    mt, d, r, _, restart = make(tmp_path, monkeypatch)
    mt.i.trade_stops_level = 100
    mt.tick(1999.86)  # 1999.92 ask, above 1999.88 but less than .10 away
    assert d.batch(NAME, r) == WAITING and not mt.requests and not d.retest.active
    if case == "restart":
        assert (
            restart(json.loads(d.path.read_text())).batch(NAME, r) == "already_consumed"
        )
        return
    mt.now += 10 if case == "late" else 0.33
    mt.tick(1999.8 if case == "touch_first" else 2000)
    outcome = d.resume_first_quote(NAME, 5)
    if case == "becomes_legal":
        assert outcome == "retest_batch_placed" and len(mt.orders) == 10
    else:
        assert not mt.requests and outcome in (
            "retest_already_touched",
            "entry_window_expired",
        )


@pytest.mark.parametrize("lag", ["orders", "charged_margin", "orders_and_tight_cap"])
def test_ten_placements_do_not_wait_for_publication_or_double_charge(
    tmp_path, monkeypatch, lag
):
    mt, d, r, _, _ = make(tmp_path, monkeypatch)
    mt.a.limit_orders = 10
    if lag.startswith("orders"):
        mt.orders_get = lambda: []
        if lag.endswith("cap"):
            d.lim = d.lim.model_copy(
                update={"max_positions": 10, "max_total_lots": 1.0}
            )
    else:
        mt.a.margin_free = 800

    def after(_):
        if lag == "charged_margin":
            mt.a.margin_initial = len(mt.orders) * 80
            mt.a.margin_free = 800 - mt.a.margin_initial
        mt.now += 0.6
        mt.tick()

    mt.after_send = after
    assert d.batch(NAME, r) == "retest_batch_placed"
    assert len(mt.orders) == 10 and not mt.positions
    assert max(x.time_setup_msc / 1000 - T for x in mt.orders) == pytest.approx(
        5.5, abs=0.002
    )


@pytest.mark.parametrize(
    ("rate", "slots", "wanted"),
    [
        ("medium", 3, 3),
        ("medium", 4, 4),
        ("fast", 6, 6),
        ("fast", 7, 7),
        ("slow", 9, 0),
    ],
)
def test_rate_selection_honours_broker_pending_slots(
    tmp_path, monkeypatch, rate, slots, wanted
):
    mt, d, r, _, _ = make(
        tmp_path,
        monkeypatch,
        hook=lambda c: c["hull"]["symbols"]["XAUUSD"].update(rate=rate),
    )
    mt.a.limit_orders = slots
    d.batch(NAME, r)
    assert len(mt.orders) == wanted


@pytest.mark.parametrize(
    "change",
    ["foreign_pending", "funding", "stale", "spec", "kill", "spread", "bad_margin"],
)
def test_working_safety_removes_only_verified_pending(tmp_path, monkeypatch, change):
    mt, d, r, _, _ = make(tmp_path, monkeypatch)
    d.batch(NAME, r)
    if change == "foreign_pending":
        mt.orders.append(NS(ticket=302))
    elif change == "funding":
        mt.a.margin_free = 20
    elif change == "stale":
        mt.now += 8
    elif change == "spec":
        mt.i.trade_tick_size = 0.005
    elif change == "kill":
        d.kill = tmp_path / "KILL"
        d.kill.touch()
    elif change == "bad_margin":
        mt.a.margin_free = float("nan")
    else:
        mt.tick(spread=0.31)
    d.supervise()
    d.supervise()
    assert (
        not d.retest.active
        and not mt.positions
        and len(mt.orders) == int(change == "foreign_pending")
    )
    assert all(q.get("order") != 302 for q in mt.requests)


def test_fill_during_dispatch_leaves_legal_siblings_working_no_topup(
    tmp_path, monkeypatch
):
    mt, d, r, logs, _ = make(tmp_path, monkeypatch)

    def before_next(_):
        if len(mt.orders) == 4:
            mt.fill(mt.orders[0])

    mt.after_send = before_next
    out = d.batch(NAME, r)
    assert out == "retest_activated_during_dispatch" and len(mt.orders) == 3
    d.supervise()
    assert len(mt.orders) == 3 and len(d.ladders.book) == 1
    assert len([x for x in logs if x["event"] == "retest_fill_receipt"]) == 1


def test_native_runner_opposing_trigger_cancels_rest_and_exits_actual_fill(
    tmp_path, monkeypatch
):
    broker = PendingMT()

    def phase(mt, ix, at):
        mt.now = at
        if ix == 2:
            assert len(mt.orders) == 4
            # Actual activation before the next scanner snapshot. Price then flips the owning M10.
            mt.fill(mt.orders[-1])

    mt, rows, _, _ = tape(
        tmp_path,
        monkeypatch,
        parents=(15,),
        broker=broker,
        route=RETEST,
        case="other_then_own",
        on_phase=phase,
    )
    assert len([q for q in mt.requests if q["action"] == 5]) == 4
    assert not mt.orders and not mt.positions
    assert any(z["event"] == "retest_fill_receipt" for z in rows)
    assert any(
        z["event"] == "exit_request"
        and z["reason"] in ("opposite_pair_trigger_hull", "opposite_hull_label")
        for z in rows
    )
    assert any(
        z["event"] == "retest_cancel_intent"
        and z["reason"] in ("opposite_pair_trigger_hull", "opposite_hull_label")
        for z in rows
    )


def test_unpublished_history_holds_then_proves_exact_deal(tmp_path, monkeypatch):
    mt, d, r, logs, _ = make(tmp_path, monkeypatch)
    d.batch(NAME, r)
    o = mt.orders[0]
    mt.now += 1
    mt.tick()
    mt.fill(o)
    ds = mt.deals
    mt.deals = []
    d.supervise()
    mt.now += 11
    mt.tick()
    d.supervise()
    assert d.retest.active[NAME]["review"] and not d.ladders.book
    mt.deals = ds
    d.supervise()
    assert not mt.positions and any(x["event"] == "retest_fill_receipt" for x in logs)


@pytest.mark.parametrize("shadow", [False, True])
def test_offline_report_acks_and_touches_are_not_fills(tmp_path, monkeypatch, shadow):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    from report_hull_retest import report

    mt, d, r, logs, _ = make(tmp_path, monkeypatch, armed=not shadow)
    d.batch(NAME, r)
    mt.now += 20
    mt.tick(1999.8)
    if not shadow:
        mt.fill(mt.orders[0], flat=True)
        [mt.terminate(z, 6) for z in list(mt.orders)]
    d.supervise()
    head = [
        {"event": "started", "summary": {"entry_route": RETEST}},
        {
            "event": "pair_arbitration",
            "symbol": NAME,
            "close_s": T,
            "owner_tf": 5,
            "outcome": "selected",
        },
    ]
    lines = [json.dumps(e) + "\n" for e in head + logs]
    result = report(lines + ['{"event":'], details=True)
    c = result["counts"]
    assert (
        result["ignored_unfinished_last_line"]
        and c["selected_signals"] == 1
        and not c["selected_without_plan"]
    )
    assert (
        c["shadow_observed_touches"] == int(shadow)
        and c["legs_with_full_volume"] == int(not shadow)
        and c["acknowledged_orders_in_log"] == (0 if shadow else 10)
    )
    assert c["legs_with_fill_receipts"] == int(not shadow) and (
        shadow or result["signals"][0]["settled"]
    )


def test_frozen_cancel_does_not_delay_partial_position_protection(
    tmp_path, monkeypatch
):
    mt, d, r, logs, _ = make(tmp_path, monkeypatch)
    d.batch(NAME, r)
    mt.now += 1
    mt.tick()
    o = mt.orders[0]
    p = mt.fill(o, qty=0.04)
    send = mt.order_send

    def reject_remove(req):
        if req["action"] == 8 and req["order"] == o.ticket:
            mt.requests.append(dict(req))
            return NS(retcode=10029)  # frozen
        return send(req)

    mt.order_send = reject_remove
    d.supervise()
    assert (
        o in mt.orders
        and not mt.positions
        and not d.ladders.book
        and d.retest.active[NAME]["review"]
    )
    assert any(
        req.get("position") == p.ticket and req["action"] == 1 for req in mt.requests
    )
    mt.terminate(o, 6)
    d.supervise()
    assert (
        not d.retest.active
        and len([x for x in logs if x["event"] == "retest_fill_receipt"]) == 1
    )


def test_unexpected_expiry_or_target_not_silently_adopted(tmp_path, monkeypatch):
    mt, d, r, _, _ = make(tmp_path, monkeypatch)
    d.batch(NAME, r)
    mt.now += 1
    mt.tick()
    o = mt.orders[0]
    p = mt.fill(o)
    p.tp += 1
    d.supervise()
    d.supervise()
    assert not mt.positions and not mt.orders and not d.ladders.book


@pytest.mark.parametrize("route_mode", ["m1_parent_structure", "parent_close"])
def test_legacy_mode_cannot_enable_retest(route_mode):
    cfg = load_yaml(ROOT / "config.yaml")["hull"]
    cfg.update(
        execution_mode=route_mode, entry_route=RETEST, eligible_timeframes=["M5"]
    )
    with pytest.raises(ValueError, match="requires paired_structure"):
        HullConfig.model_validate(cfg)


def test_original_route_and_profile_required_on_restart(tmp_path, monkeypatch):
    mt, d, r, _logs, restart = make(tmp_path, monkeypatch)
    d.batch(NAME, r)
    state = json.loads(d.path.read_text())
    d2 = restart(state)
    d2.h = d2.h.model_copy(update={"retest_fraction": 0.25})
    with pytest.raises(DemoSafetyError, match="original account, profile"):
        d2.retest.recover([5])
    with pytest.raises(DemoSafetyError, match="original manager"):
        Desk(
            mt,
            {"run": {"mode": "demo"}},
            d.h.model_copy(update={"entry_route": "market_next_quote"}),
            d.syms,
            (1, "t"),
            state,
            d.path,
            d.emit,
            True,
        )
    assert len(mt.orders) == 10


def test_preflight_requires_server_expiry_never_uses_gtc(tmp_path, monkeypatch):
    mt = PendingMT()
    mt.i.expiration_mode = 1
    with pytest.raises(DemoSafetyError, match="capability"):
        tape(
            tmp_path, monkeypatch, broker=mt, route=RETEST, armed=False, preflight=True
        )
    assert mt.requests == []
