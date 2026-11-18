"""No network, credentials, MT5 installation or money are used by these tests."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import pytest

from bot.config.loader import load_yaml
from bot.execution.demo_runner import DemoSafetyError
from bot.zones.config import ZoneConfig
from bot.zones.engine import Decision
from bot.zones.monitor import Desk, monitor

T = int(datetime(2026, 11, 3, 12, tzinfo=UTC).timestamp())


class Terminal:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    POSITION_TYPE_BUY = ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    TRADE_ACTION_DEAL = 1
    SYMBOL_TRADE_MODE_DISABLED = 0
    SYMBOL_TRADE_MODE_CLOSEONLY = 3
    SYMBOL_TRADE_MODE_SHORTONLY = 2
    SYMBOL_TRADE_MODE_LONGONLY = 1
    TIMEFRAME_M1 = 1
    ORDER_FILLING_FOK = 0
    TRADE_RETCODE_DONE = 10009

    def __init__(self):
        self.account = NS(
            login=1,
            server="test-demo",
            trade_mode=0,
            margin_mode=2,
            currency="USD",
            trade_allowed=True,
            trade_expert=True,
            equity=10000,
        )
        self.tick = NS(bid=4000.0, ask=4000.24, time_msc=T * 1000 + 100)
        self.info = NS(
            point=0.001,
            trade_tick_size=0.001,
            trade_contract_size=100,
            currency_profit="USD",
            currency_base="XAU",
            volume_min=0.01,
            volume_max=100,
            volume_step=0.01,
            trade_mode=4,
            trade_stops_level=0,
            trade_freeze_level=0,
            filling_mode=1,
        )
        self.orders = []
        self.positions = []
        self.sent = []
        self.selected = []
        self.rate_requests = []
        self.stopped = False

    def account_info(self):
        return self.account

    def terminal_info(self):
        return NS(connected=True, trade_allowed=True)

    def positions_get(self, ticket=None):
        return (
            self.positions
            if ticket is None
            else [p for p in self.positions if p.ticket == ticket]
        )

    def orders_get(self):
        return self.orders

    def symbol_info_tick(self, name):
        return self.tick

    def symbol_info(self, name):
        return (
            self.info
            if name == "XAUUSDm"
            else NS(
                **{
                    **vars(self.info),
                    "point": 0.00001,
                    "trade_tick_size": 0.00001,
                    "trade_contract_size": 100000,
                }
            )
        )

    def order_calc_profit(self, typ, name, lots, entry, end):
        return (1 if typ == 0 else -1) * (end - entry) * 100 * lots

    def order_check(self, r):
        return NS(retcode=0, margin_free=9900)

    def order_send(self, r):
        self.sent.append(r.copy())
        i = len(self.sent)
        self.positions.append(
            NS(
                ticket=i,
                symbol=r["symbol"],
                magic=r["magic"],
                volume=r["volume"],
                type=r["type"],
                price_open=r["price"],
                sl=r["sl"],
                tp=r["tp"],
            )
        )
        return NS(retcode=10009, volume=r["volume"], price=r["price"], deal=i)

    def initialize(self):
        return True

    def shutdown(self):
        self.stopped = True

    def symbol_select(self, name, on):
        self.selected.append(name)
        return True

    def copy_rates_from_pos(self, name, tf, index, n):
        self.rate_requests.append((name, index))
        return [
            {
                "time": T - 60 * (241 - i),
                "open": 4000,
                "high": 4001,
                "low": 3999,
                "close": 4000,
            }
            for i in range(241)
        ]


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from pathlib import Path

    cfg = load_yaml(Path(__file__).resolve().parents[1] / "config.yaml")
    cfg["run"]["mode"] = "demo"
    cfg["zones"]["symbols"]["XAUUSD"]["legs"] = 2
    mt = Terminal()
    monkeypatch.setattr("bot.zones.monitor.time.time", lambda: T + 0.2)
    z = ZoneConfig.model_validate(cfg["zones"])
    path = tmp_path / "state.json"
    desk = Desk(mt, cfg, z, (1, "test-demo"), {}, path, lambda *a: None)
    desk.history_ok = True
    return desk, z.symbols["XAUUSD"], Decision(T, "sample", 1, 1)


def test_batch_owned_stops_and_restart_idempotence(desk):
    d, s, sig = desk
    assert d.send(s, sig) == "demo_batch_sent" and len(d.mt.sent) == 2
    assert all(
        r["sl"] < r["price"] < r["tp"]
        and r["volume"] == 0.09
        and r["magic"] == d.lim.magic
        for r in d.mt.sent
    )
    assert all(r["deviation"] == 0 for r in d.mt.sent)
    d.mt.positions.clear()
    d.state = json.loads(d.path.read_text())
    assert d.send(s, sig) == "already_attempted"


def test_uncertain_batch_never_blindly_retried(desk, monkeypatch):
    d, s, sig = desk
    monkeypatch.setattr(d.mt, "order_send", lambda r: None)
    assert d.send(s, sig) == "incomplete_or_unverified_batch_manual_hold"
    assert json.loads(d.path.read_text())["manual_hold"]["leg"] == 1
    assert d.send(s, Decision(T + 60, "another", 1)) == "reconcile_uncertain_send"


@pytest.mark.parametrize("kind", ["real", "paper", "switched", "netting", "not_usd"])
def test_arming_never_accepts_wrong_account(desk, kind):
    d, s, sig = desk
    if kind == "real":
        d.mt.account.trade_mode = 2
    elif kind == "netting":
        d.mt.account.margin_mode = 0
    elif kind == "paper":
        d.mode = "paper"
    elif kind == "switched":
        d.mt.account.login += 1
    else:
        d.mt.account.currency = "JPY"
    with pytest.raises(DemoSafetyError):
        d.send(s, sig)
    assert not d.mt.sent


@pytest.mark.parametrize(
    ("case", "why"),
    [
        ("stale", "stale_signal_or_quote"),
        ("kill", "kill"),
        ("foreign", "foreign_or_unprotected_exposure"),
        ("margin", "margin_floor"),
        ("spec", "broker_contract_point_or_tick_mismatch"),
        ("history", "history_unavailable"),
        ("rate", "order_rate_limit"),
    ],
)
def test_demo_rejects_without_sending(desk, case, why, monkeypatch):
    d, s, sig = desk
    if case == "stale":
        d.mt.tick.time_msc -= 20000
    if case == "kill":
        d.kill.touch()
    if case == "foreign":
        d.mt.positions.append(NS(symbol="OTHER", magic=99, volume=0.1))
    if case == "margin":
        monkeypatch.setattr(
            d.mt, "order_check", lambda r: NS(retcode=0, margin_free=100)
        )
    if case == "spec":
        d.mt.info.trade_contract_size = 1
    if case == "history":
        d.history_ok = False
    if case == "rate":
        d.state["entry_sends"] = [T] * 40
    assert d.send(s, sig) == why and d.mt.sent == []


def test_shadow_scans_user_list_but_never_sends_or_flattens(tmp_path, monkeypatch):
    cfg = load_yaml("config.yaml")
    cfg["zones"]["symbols"]["EURUSD"]["enabled"] = True
    mt = Terminal()
    mt.account.trade_mode = (
        2  # observation is read-only even when terminal points to real
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "KILL").touch()
    monkeypatch.setattr("bot.zones.monitor.time.time", lambda: T + 0.2)
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt)
    monitor(cfg, armed=False, iterations=1)
    assert mt.stopped and mt.sent == [] and mt.selected == ["XAUUSDm", "EURUSDm"]
    assert mt.rate_requests == [("XAUUSDm", 1), ("EURUSDm", 1)]
    assert not list(tmp_path.rglob("state.json"))
    audit = next(tmp_path.rglob("shadow.jsonl"))
    assert len(audit.read_text().splitlines()) == 3


def test_kill_does_not_close_foreign_tickets(desk):
    d, s, _sig = desk
    d.close(NS(symbol=s.broker_symbol, magic=d.lim.magic + 1), "kill")
    assert d.mt.sent == []
