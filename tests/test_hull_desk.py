from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_cases import PATH, sequence
from hull_mt import MT, ROOT, T, sig

from bot.config.loader import load_yaml
from bot.execution.demo_runner import DemoSafetyError
from bot.hull.config import HullConfig, selection
from bot.hull.desk import identity_check, resolve
from bot.hull.entry_clock import WAITING
from bot.hull.orders import Block, Grid, target
from bot.hull.terminal import Stream, run


@pytest.mark.parametrize(
    ("rate", "sl", "legs"),
    [
        ("fast", 1999.95, 7),
        ("fast", 1999.85, 7),
        ("medium", 1999.80, 4),
        ("medium", 1999.65, 4),
        ("slow", 1999.1, 10),
    ],
)
def test_exact_batch_range_per_ticket_tp_dynamic_sl_no_five_dollar_clip(
    hull_desk, rate, sl, legs
):
    mt, d, logs = hull_desk(rate)
    s = sig(sl)
    assert d.batch("XAUUSDm", s) == "demo_batch_sent"
    assert len(mt.positions) == legs
    for p in mt.positions:
        assert p.sl == sl and p.volume == 0.1 and p.magic == d.h.magic
        assert (p.tp - p.price_open) * 10 >= 10 and (
            p.tp - 0.001 - p.price_open
        ) * 10 < 10
    if rate == "slow":
        assert (mt.q.ask - sl) * 10 > 5
    assert len(d.state["target_by_ticket"]) == legs and not d.state.get("manual_hold")
    assert d.batch("XAUUSDm", s) == "already_consumed"
    assert any(x[0] == "batch_preview" for x in logs)


def test_nothing_sent_if_batch_minimum_does_not_fit(hull_desk):
    mt, d, log = hull_desk("fast")
    d.lim = d.lim.model_copy(update={"max_open_risk_usd": 0.1})
    assert d.batch("XAUUSDm", sig(1999.1)) == "portfolio_risk_limit"
    veto = next(data for name, data in log if name == "risk_veto")
    assert veto["min_legs"] == 6 and veto["min_batch_risk"] == pytest.approx(60)
    assert not mt.requests


def test_shadow_and_unknown_ack_never_resubmit(hull_desk):
    mt, d, _ = hull_desk(armed=False)
    assert d.batch("XAUUSDm", sig()) == "shadow_plan_only" and not mt.requests
    mt, d, _ = hull_desk("fast")
    mt.bad = True
    assert "manual_hold" in d.batch("XAUUSDm", sig())
    assert len(mt.requests) == 1
    _, d2, _ = hull_desk("fast", state=json.loads(d.path.read_text()), mt=mt)
    assert d2.batch("XAUUSDm", sig()) == "already_consumed"
    assert (
        d2.batch("XAUUSDm", replace(sig(), close_s=T + 60))
        == "manual_reconciliation_required"
    )
    assert len(mt.requests) == 1


def test_fill_retargets_to_actual_open_and_target_is_not_a_momentum_exit(hull_desk):
    mt, d, _ = hull_desk()
    mt.retprice = 2000.12
    assert d.batch("XAUUSDm", sig()) == "demo_batch_sent"
    assert (
        len(mt.positions) == 10
        and len(mt.requests) == 20
        and mt.requests[0]["action"] == 1
        and mt.requests[1]["action"] == 6
        and mt.positions[0].tp == pytest.approx(2001.12)
    )
    mt.positions[0].profit = 9.99
    d.supervise()
    assert len(mt.positions) == 10
    mt.positions[0].profit = 10.0
    d.supervise()
    assert len(mt.positions) == 9


def test_opposite_hull_label_closes_held_side_before_reversal(hull_desk):
    mt, d, _ = hull_desk("slow")
    buy = replace(sig(side=1), close_s=T - 1)
    assert d.batch("XAUUSDm", buy) == "demo_batch_sent"
    assert len(mt.positions) == 10

    sell = replace(sig(sl=2000.05, side=-1), close_s=T)
    assert d.close_opposite("XAUUSDm", sell) == 10
    assert not mt.positions


def test_protection_entry_age_foreign_positions_and_kill_ownership(hull_desk):
    mt, d, _ = hull_desk()
    mt.q.time_msc -= 10000
    assert d.batch("XAUUSDm", sig()) == WAITING
    assert not mt.requests and "XAUUSDm" in d.waiting
    mt, d, _ = hull_desk()
    mt.positions = [NS(ticket=1, symbol="XAUUSDm", magic=99, sl=1, tp=2)]
    assert d.batch("XAUUSDm", sig()) == "foreign_or_unprotected_exposure"
    d.kill.touch()
    d.supervise()
    assert not mt.requests
    mt, d, _ = hull_desk()
    assert d.batch("XAUUSDm", sig()) == "kill"
    d.kill.unlink()
    mt, d, _ = hull_desk()
    mt.i.trade_stops_level = 200
    assert (
        d.batch("XAUUSDm", sig()) == "swing_breached_or_too_close" and not mt.requests
    )
    mt, d, _ = hull_desk()
    mt.orders.append(1)
    assert d.batch("XAUUSDm", sig()) == "pending_orders_present"


def test_conflicting_broker_aliases_bad_lots_or_real_account_fail_closed(hull_desk):
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


def test_uncertain_second_leg_does_not_retry_or_open_remaining_tickets(hull_desk):
    mt, d, _ = hull_desk("fast")
    send = mt.order_send

    def second_fails(r):
        if mt.requests:
            mt.bad = True
        return send(r)

    mt.order_send = second_fails
    assert "manual_hold" in d.batch("XAUUSDm", sig())
    assert len(mt.requests) == 2 and len(mt.positions) == 1 and mt.positions[0].sl > 0
    assert d.batch("XAUUSDm", sig()) == "already_consumed"


def test_final_quote_changed_between_precheck_and_send(hull_desk):
    mt, d, _ = hull_desk("medium")
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


def test_drawdown_halt_blocks_new_batch_without_resetting_swing(hull_desk):
    mt, d, _ = hull_desk()
    d.mark(mt.a, T)
    mt.a.equity -= 80
    assert d.batch("XAUUSDm", sig()) == "drawdown_halt" and not mt.requests


@pytest.mark.parametrize(
    "eligible,exit_opposite", [(True, True), (False, True), (False, False)]
)
def test_cli_native_m2_entry_filter_and_independent_exit(
    tmp_path, monkeypatch, eligible, exit_opposite
):
    """Through the real CLI/coordinator, not by submitting our own decision to Desk."""
    import sys

    import yaml

    from bot.cli import main
    from bot.hull.signal import Hull
    from bot.zones.engine import Bar

    cfg = load_yaml(ROOT / "config.yaml")
    cfg["run"]["mode"] = "demo"
    cfg["hull"]["indicator"]["exit_opposite"] = exit_opposite
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
            and bool(d.entry_side) == eligible
            and (
                not eligible
                or not d.structure.veto(
                    d.side, r["close"] + (0.01 if d.side == 1 else 0)
                )
            )
            and d.raw_sl is not None
            and 0.1 < d.side * (r["close"] - d.raw_sl) < 14
        ):
            idx = i
            break
    assert idx is not None
    if not eligible:
        mt.positions.append(
            NS(
                ticket=91,
                magic=h.magic,
                symbol="XAUUSDm",
                type=mt.ORDER_TYPE_SELL if d.side == 1 else mt.ORDER_TYPE_BUY,
                sl=d.close + 10 * d.side,
                tp=d.close - 10 * d.side,
                volume=0.01,
                price_open=d.close,
                profit=0.0,
            )
        )
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
    assert main() == 0 and calls == [2, 2]
    ledger = [
        json.loads(l)
        for l in next(tmp_path.glob("reports/hull_runtime/*/demo.jsonl"))
        .read_text()
        .splitlines()
    ]
    (record,) = [r for r in ledger if r["event"] == "closed_label"]
    assert record["label"] == d.label and record["entry_side"] == d.entry_side
    if eligible:
        assert len(mt.positions) == len(mt.requests) == 10
        assert all(p.sl > 0 and p.tp > 0 and p.volume == 0.01 for p in mt.positions)
        assert ledger[-1]["outcome"] == "demo_batch_sent"
    else:
        assert ledger[-1]["outcome"] == d.entry_reason and not ledger[-1]["tf"]
        assert not [r for r in mt.requests if "position" not in r]
        assert len(mt.positions) == int(not exit_opposite)
        assert [r.get("position") for r in mt.requests] == (
            [91] if exit_opposite else []
        )


@pytest.mark.parametrize("bias", [1, -1])
def test_actual_hull_label_beyond_confirmed_bos_opens_protected_batch(hull_desk, bias):
    mt, desk, _ = hull_desk()
    row = sequence(bias, closes=PATH[:21], end_s=T)[-1]
    assert row.label == row.entry_side == -bias and row.structure.event == "bos"
    mt.q.bid = row.close
    mt.q.ask = row.close + 0.1
    assert desk.batch("XAUUSDm", row) == "demo_batch_sent"
    assert len(mt.positions) == 10
    for p in mt.positions:
        assert p.type == (mt.ORDER_TYPE_SELL if bias == 1 else mt.ORDER_TYPE_BUY)
        assert p.volume == 0.10 and p.tp > 0 and p.sl > 0


@pytest.mark.parametrize("bad_close", [False, True])
@pytest.mark.parametrize("bias", [1, -1])
def test_bos_close_and_executable_quote_both_must_pass(hull_desk, bias, bad_close):
    row = sequence(bias, closes=PATH[:21], end_s=T)[-1]
    edge = row.structure.broken.price
    mt, desk, _ = hull_desk()
    ask_offset = 0.1 if row.side == 1 else 0.0
    # Close valid but executable quote exactly on BOS; or close invalid but
    # quote comes back inside. Neither case may result in a single ticket.
    at = row.close if bad_close else edge
    mt.q.bid, mt.q.ask = at - ask_offset, at + 0.1 - ask_offset
    if bad_close:
        row = replace(row, close=edge)
    why = desk.batch("XAUUSDm", row)
    assert (
        why
        == ("" if bad_close else "quote_")
        + "outside_"
        + row.structure.band(row.side).name
    )
    assert not mt.requests and desk.batch("XAUUSDm", row) == "already_consumed"


def test_each_batch_leg_rechecks_frozen_zone_and_does_not_top_up(hull_desk):
    mt, desk, _ = hull_desk("medium")
    d = sig()
    # First ask=2000.10 at the inclusive midpoint; next ask a tick beyond it.
    s = replace(
        d.structure,
        pullback=replace(d.structure.pullback, price=1999.9),
        extreme=replace(d.structure.extreme, price=2000.3),
    )
    d = replace(d, structure=s)
    send = mt.order_send

    def drift(request):
        ack = send(request)
        mt.q.bid, mt.q.ask = 2000.001, 2000.101
        return ack

    mt.order_send = drift
    assert (
        desk.batch("XAUUSDm", d)
        == "protected_partial_no_topup:quote_outside_bull_discount_buy"
    )
    assert (
        len(mt.positions) == len(mt.requests) == 1
        and mt.positions[0].sl > 0
        and mt.positions[0].tp > 0
    )
    assert desk.batch("XAUUSDm", d) == "already_consumed"


@pytest.mark.parametrize("held", [False, True])
def test_flat_may_use_only_qualified_lane_but_raw_conflict_never_exits(
    tmp_path, monkeypatch, held
):
    """Exercise coordinator when the old exit selector and new entry selector differ."""
    import sys

    from bot.hull.structure import Structure

    cfg = load_yaml(ROOT / "config.yaml")
    cfg["run"]["mode"] = "demo"
    h = HullConfig.model_validate(cfg["hull"])
    mt = MT()
    mt.initialize = lambda: True
    mt.shutdown = lambda: None
    mt.history_deals_get = lambda *args: []
    good = sig()
    noise = replace(sig(sl=2000.5, side=-1), tf=5, structure=Structure())
    if held:
        mt.positions = [
            NS(
                ticket=55,
                symbol="XAUUSDm",
                magic=h.magic,
                type=mt.ORDER_TYPE_SELL,
                sl=2000.5,
                tp=1999.0,
                volume=0.09,
                price_open=2000.0,
                profit=0.0,
            )
        ]
    monkeypatch.setattr(
        Stream, "refresh", lambda lane, *_: (good if lane.tf == 1 else noise, "updated")
    )
    monkeypatch.setattr("bot.hull.terminal.time.time", lambda: T + 0.3)
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    run(cfg, h, selection(h), (1, 5), armed=True, iterations=1)
    ledger = [
        json.loads(row)
        for row in next(tmp_path.glob("reports/hull_runtime/*/demo.jsonl"))
        .read_text()
        .splitlines()
    ]
    last = ledger[-1]
    assert last["raw_selected_tf"] is None and last["tf"] == 1
    if held:
        assert last["outcome"] == "symbol_busy_or_batch_changed" and not mt.requests
        assert mt.positions[0].ticket == 55
    else:
        assert last["outcome"] == "demo_batch_sent" and len(mt.requests) == 10
        assert mt.positions[0].type == mt.ORDER_TYPE_BUY
