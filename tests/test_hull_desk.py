from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from bot.config.loader import load_yaml
from bot.execution.demo_runner import DemoSafetyError
from bot.hull.config import HullConfig, selection
from bot.hull.desk import Desk, identity_check, resolve
from bot.hull.orders import Block, Grid, target
from bot.hull.signal import Reading
from bot.hull.terminal import Stream, run

T = 1794312000  # Tue 12:00 UTC
ROOT = Path(__file__).resolve().parents[1]


class MT:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    POSITION_TYPE_BUY = ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    TRADE_RETCODE_DONE = 10009
    SYMBOL_TRADE_MODE_DISABLED = 0
    SYMBOL_TRADE_MODE_CLOSEONLY = 3
    SYMBOL_TRADE_MODE_SHORTONLY = 2
    SYMBOL_TRADE_MODE_LONGONLY = 1
    TIMEFRAME_M1 = 1
    TIMEFRAME_M2 = 2
    TIMEFRAME_M5 = 5

    def __init__(self):
        self.a = NS(
            connected=True,
            login=1,
            server="t",
            trade_mode=0,
            margin_mode=2,
            currency="USD",
            trade_allowed=True,
            trade_expert=True,
            equity=10000.0,
            margin_free=9000.0,
        )
        self.i = NS(
            name="XAUUSDm",
            trade_tick_size=0.001,
            point=0.001,
            digits=3,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            trade_stops_level=0,
            trade_freeze_level=0,
            trade_mode=4,
            filling_mode=1,
            trade_contract_size=100,
            currency_profit="USD",
        )
        self.q = NS(time_msc=T * 1000 + 100, bid=2000.0, ask=2000.1)
        self.positions = []
        self.orders = []
        self.catalog = [self.i]
        self.requests = []
        self.retprice = None
        self.bad = False

    def account_info(self):
        return self.a

    def terminal_info(self):
        return self.a

    def symbol_info(self, n):
        return self.i

    def symbol_select(self, *args):
        return True

    def symbols_get(self):
        return self.catalog

    def symbol_info_tick(self, n):
        return self.q

    def positions_get(self, ticket=None):
        return [p for p in self.positions if ticket is None or p.ticket == ticket]

    def orders_get(self):
        return self.orders

    def order_calc_profit(self, tp, n, l, a, b):
        return (1 if tp == 0 else -1) * (b - a) * l * 100

    def order_calc_margin(self, *args):
        return 80.0

    def order_check(self, r):
        return NS(retcode=0, margin_free=8900.0)

    def order_send(self, r):
        self.requests.append(dict(r))
        ret = NS(retcode=10009)
        if self.bad:
            return None
        if "position" in r:
            p = self.positions_get(ticket=r["position"])[0]
            if r["action"] == 6:
                p.sl = r["sl"]
                p.tp = r["tp"]
            else:
                self.positions.remove(p)
            return ret
        i = len(self.requests)
        fill = self.retprice or r["price"]
        p = NS(
            ticket=i,
            magic=r["magic"],
            symbol=r["symbol"],
            type=r["type"],
            sl=r["sl"],
            tp=r["tp"],
            volume=r["volume"],
            price_open=fill,
            profit=0.0,
        )
        self.positions.append(p)
        return NS(retcode=10009, volume=r["volume"], price=fill, deal=i)


def sig(sl=1999.95, side=1):
    return Reading(
        T,
        1,
        2000.0,
        2000.0,
        2001.0,
        2002.0,
        0.4,
        side,
        side,
        "ready",
        sl + side * 0.04,
        T - 120,
        sl,
        T - 300,
        4,
        None,
        None,
    )


@pytest.fixture
def factory(tmp_path, monkeypatch):
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["run"]["mode"] = "demo"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("bot.hull.desk.time.time", lambda: T + 0.3)

    def go(rate="slow", armed=True, state=None, mt=None):
        mt = mt or MT()
        h = HullConfig.model_validate(cfg["hull"])
        prof = selection(h, ["XAUUSD"], rate=rate)
        syms = resolve(mt, prof)
        logs = []
        desk = Desk(
            mt,
            cfg,
            h,
            syms,
            (1, "t"),
            state or {},
            tmp_path / "state.json",
            lambda event, **kw: logs.append((event, kw)),
            armed,
        )
        return mt, desk, logs

    return go


@pytest.mark.parametrize(
    ("rate", "sl", "legs"),
    [
        ("fast", 1999.95, 7),
        ("fast", 1999.85, 6),
        ("medium", 1999.80, 4),
        ("medium", 1999.65, 3),
        ("slow", 1999.1, 1),
    ],
)
def test_exact_batch_range_per_ticket_tp_dynamic_sl_no_five_dollar_clip(
    factory, rate, sl, legs
):
    mt, d, logs = factory(rate)
    s = sig(sl)
    assert d.batch("XAUUSDm", s) == "demo_batch_sent"
    assert len(mt.positions) == legs
    for p in mt.positions:
        assert p.sl == sl and p.volume == 0.09 and p.magic == d.h.magic
        assert (p.tp - p.price_open) * 9 >= 10 and (
            p.tp - 0.001 - p.price_open
        ) * 9 < 10
    if rate == "slow":
        assert (mt.q.ask - sl) * 9 > 5
    assert len(d.state["target_by_ticket"]) == legs and not d.state.get("manual_hold")
    assert d.batch("XAUUSDm", s) == "already_consumed"
    assert any(x[0] == "batch_preview" for x in logs)


def test_nothing_sent_if_batch_minimum_does_not_fit(factory):
    mt, d, log = factory("fast")
    assert d.batch("XAUUSDm", sig(1999.1)) == "portfolio_risk_limit"
    veto = next(data for name, data in log if name == "risk_veto")
    assert veto["min_legs"] == 6 and veto["min_batch_risk"] == pytest.approx(54)
    assert not mt.requests


def test_shadow_and_unknown_ack_never_resubmit(factory):
    mt, d, _ = factory(armed=False)
    assert d.batch("XAUUSDm", sig()) == "shadow_plan_only" and not mt.requests
    mt, d, _ = factory("fast")
    mt.bad = True
    assert "manual_hold" in d.batch("XAUUSDm", sig())
    assert len(mt.requests) == 1
    _, d2, _ = factory("fast", state=json.loads(d.path.read_text()), mt=mt)
    assert d2.batch("XAUUSDm", sig()) == "already_consumed"
    assert (
        d2.batch("XAUUSDm", replace(sig(), close_s=T + 60))
        == "manual_reconciliation_required"
    )
    assert len(mt.requests) == 1


def test_fill_retargets_to_actual_open_and_target_is_not_a_momentum_exit(factory):
    mt, d, _ = factory()
    mt.retprice = 2000.12
    assert d.batch("XAUUSDm", sig()) == "demo_batch_sent"
    assert (
        len(mt.requests) == 2
        and mt.requests[1]["action"] == 6
        and mt.positions[0].tp == pytest.approx(2001.232)
    )
    mt.positions[0].profit = 9.99
    d.supervise()
    assert len(mt.positions) == 1
    mt.positions[0].profit = 10.0
    d.supervise()
    assert not mt.positions and mt.requests[-1]["position"] == 1


def test_protection_entry_age_foreign_positions_and_kill_ownership(factory):
    mt, d, _ = factory()
    mt.q.time_msc -= 10000
    assert d.batch("XAUUSDm", sig()) == "stale_label_or_quote"
    mt, d, _ = factory()
    mt.positions = [NS(ticket=1, symbol="XAUUSDm", magic=99, sl=1, tp=2)]
    assert d.batch("XAUUSDm", sig()) == "foreign_or_unprotected_exposure"
    d.kill.touch()
    d.supervise()
    assert not mt.requests
    mt, d, _ = factory()
    assert d.batch("XAUUSDm", sig()) == "kill"
    d.kill.unlink()
    mt, d, _ = factory()
    mt.i.trade_stops_level = 200
    assert (
        d.batch("XAUUSDm", sig()) == "swing_breached_or_too_close" and not mt.requests
    )
    mt, d, _ = factory()
    mt.orders.append(1)
    assert d.batch("XAUUSDm", sig()) == "pending_orders_present"


def test_conflicting_broker_aliases_bad_lots_or_real_account_fail_closed(factory):
    cfg = load_yaml(ROOT / "config.yaml")
    h = HullConfig.model_validate(cfg["hull"])
    p = selection(h)
    mt = MT()
    mt.catalog.append(NS(name="XAUUSD"))
    with pytest.raises(DemoSafetyError, match="exact matches"):
        resolve(mt, p)
    p["XAUUSD"] = p["XAUUSD"].model_copy(
        update={"broker_symbol": "XAUUSDm", "lots": 0.001}
    )
    with pytest.raises(Block, match="illegal_lot"):
        resolve(mt, p)
    mt.a.trade_mode = 2
    with pytest.raises(DemoSafetyError):
        identity_check(mt, "demo", True)
    with pytest.raises(DemoSafetyError):
        run(cfg, h, p, armed=True)


def test_select_profiles_and_fx_currency_conversion():
    h = HullConfig.model_validate(load_yaml(ROOT / "config.yaml")["hull"])
    s = selection(h, profiles=["XAUUSD:0.09:10:fast", "USDJPY:0.02:8:slow"])
    assert s["USDJPY"].target_usd == 8 and s["XAUUSD"].rate == "fast"
    with pytest.raises(ValueError):
        selection(h, ["EURUSD"], ["XAUUSD:0.09:10:fast"])
    with pytest.raises(ValueError):
        selection(h, ["NOT_A_SYMBOL"])
    g = Grid(0.001, 0.001, 3, 0.001, 0.01, 100, 0.01)
    cash = lambda side, lots, entry, px: side * (px - entry) * 100000 * lots / px
    for side in (-1, 1):
        t, val = target(g, side, 0.02, 152.032, 8.0, cash)
        assert val >= 8 and cash(side, 0.02, 152.032, t - side * g.tick) < 8
    with pytest.raises(Block):
        target(g, 1, 0.02, 152.032, 8.0, lambda *a: None)


def test_stream_ignores_forming_bar_and_never_replays_startup_labels():
    from bot.hull.config import Indicator

    fix = json.loads((ROOT / "research/hull_labels/reference.json").read_text())
    j = next(i for i, r in enumerate(fix["sets"][0]["rows"]) if i > 80 and r[2])
    tf = 2
    dt = tf * 60
    data = [dict(time=T + i * dt, **r) for i, r in enumerate(fix["bars"])]
    end = [j]
    calls = []

    def rates(n, period, shift, count):
        calls.append((period, shift))
        return data[max(0, end[0] - count) : end[0]]

    mt = NS(TIMEFRAME_M2=2, copy_rates_from_pos=rates)
    s = Stream(2, Indicator(), 0.01)
    assert s.refresh(mt, "S", T + j * dt + 0.2) == (None, "warmup_only")
    end[0] = j + 2
    r, _ = s.refresh(mt, "S", T + (j + 1) * dt + 0.2)
    assert r.label == fix["sets"][0]["rows"][j][2] and r.close_s == T + (j + 1) * dt
    assert s.refresh(mt, "S", T + (j + 1) * dt + 0.3)[0] is None and all(
        pair == (2, 1) for pair in calls
    )


def test_uncertain_second_leg_does_not_retry_or_open_remaining_tickets(factory):
    mt, d, _ = factory("fast")
    send = mt.order_send

    def second_fails(r):
        if mt.requests:
            mt.bad = True
        return send(r)

    mt.order_send = second_fails
    assert "manual_hold" in d.batch("XAUUSDm", sig())
    assert len(mt.requests) == 2 and len(mt.positions) == 1 and mt.positions[0].sl > 0
    assert d.batch("XAUUSDm", sig()) == "already_consumed"


def test_final_quote_changed_between_precheck_and_send(factory):
    mt, d, _ = factory("medium")
    base = mt.order_check

    def revised(req):
        mt.q = NS(time_msc=T * 1000 + 105, bid=2000.03, ask=2000.13)
        return base(req)

    mt.order_check = revised
    assert d.batch("XAUUSDm", sig()) == "quote_or_plan_changed" and not mt.requests


def test_preflight_resolves_only_requested_symbols_and_never_sends(
    tmp_path, monkeypatch, capsys
):
    import sys

    mt = MT()
    cfg = load_yaml(ROOT / "config.yaml")
    h = HullConfig.model_validate(cfg["hull"])
    mt.copy_rates_from_pos = lambda *args: []
    mt.initialize = lambda: True
    mt.shutdown = lambda: None
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt)
    monkeypatch.chdir(tmp_path)
    answer = run(cfg, h, selection(h), preflight=True)
    assert (
        answer["timeframes"] == [1, 2, 5]
        and list(answer["specs"]) == ["XAUUSDm"]
        and not mt.requests
    )
    assert answer["mode"] == "READ ONLY" and not list(tmp_path.glob("reports/*"))


def test_drawdown_halt_blocks_new_batch_without_resetting_swing(factory):
    mt, d, _ = factory()
    d.mark(mt.a, T)
    mt.a.equity -= 80
    assert d.batch("XAUUSDm", sig()) == "drawdown_halt" and not mt.requests


def test_cli_native_m2_full_loop_to_one_protected_mock_ticket(tmp_path, monkeypatch):
    """Through the real CLI/coordinator, not by submitting our own decision to Desk."""
    import sys

    import yaml

    from bot.cli import main
    from bot.hull.signal import Hull
    from bot.zones.engine import Bar

    cfg = load_yaml(ROOT / "config.yaml")
    cfg["run"]["mode"] = "demo"
    h = HullConfig.model_validate(cfg["hull"])
    mt = MT()
    step = 120
    raw = json.loads((ROOT / "research/hull_labels/reference.json").read_text())["bars"]
    engine = Hull(2, h.indicator, 0.01)
    idx = None
    for i, r in enumerate(raw[:-1]):
        d = engine.update(Bar(i * step, r["open"], r["high"], r["low"], r["close"]))
        if (
            i > 60
            and d.side
            and d.raw_sl is not None
            and 0.1 < d.side * (r["close"] - d.raw_sl) < 14
        ):
            idx = i
            break
    assert idx is not None
    end = [idx]
    now = [T - step + 0.3]
    origin = T - (idx + 1) * step
    calls = []

    def rates(name, tf, shift, count):
        assert tf == mt.TIMEFRAME_M2 and shift == 1
        calls.append(tf)
        return [
            dict(time=origin + k * step, **raw[k])
            for k in range(max(0, end[0] - count), end[0])
        ]

    def advance(seconds):
        end[0] += 1
        now[0] += step

    mt.copy_rates_from_pos = rates
    mt.initialize = lambda: True
    mt.shutdown = lambda: None
    mt.history_deals_get = lambda *args: []
    mt.q = NS(
        time_msc=T * 1000 + 100, bid=raw[idx]["close"], ask=raw[idx]["close"] + 0.01
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("bot.hull.terminal.time.time", lambda: now[0])
    monkeypatch.setattr("bot.hull.terminal.time.sleep", advance)
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt)
    local = tmp_path / "cfg.yaml"
    local.write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bot",
            "trade-all",
            "--config",
            str(local),
            "--timeframe",
            "M2",
            "--max-iterations",
            "2",
            "--arm-demo",
            "--profile",
            "XAUUSD:0.01:10:slow",
        ],
    )
    assert (
        main() == 0
        and len(mt.positions) == 1
        and len(mt.requests) == 1
        and calls == [2, 2]
    )
    ledger = [
        json.loads(l)
        for l in next(tmp_path.glob("reports/hull_runtime/*/demo.jsonl"))
        .read_text()
        .splitlines()
    ]
    assert (
        len([r for r in ledger if r["event"] == "closed_label"]) == 1
        and ledger[-1]["outcome"] == "demo_batch_sent"
    )
