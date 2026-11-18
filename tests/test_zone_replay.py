from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from bot.config.loader import load_yaml
from bot.zones.config import ZoneConfig
from bot.zones.engine import Bar, Decision, ZoneEngine
from bot.zones.feed import Minute, bar_minutes, tick_minutes
from bot.zones.orders import (
    EquityGuard,
    Plan,
    plan_entry,
    pnl,
    portfolio_block,
    reversal,
)
from bot.zones.replay import hit, replay

CFG = load_yaml("config.yaml")
Z = ZoneConfig.model_validate(CFG["zones"])
G = Z.symbols["XAUUSD"]
T = int(datetime(2026, 11, 3, 12, tzinfo=UTC).timestamp())


def minute(t, bid, spread=0.24, exact=True):
    b = np.array(bid, dtype=float)
    return Minute(
        Bar(t, b[0], max(b), min(b), b[-1]),
        t * 1000 + np.arange(len(b)) * 10,
        b,
        b + spread,
        exact,
        len(b),
    )


@pytest.mark.parametrize("direction", [-1, 1])
def test_requested_spread_only_usd_risk_and_return(direction):
    assert (G.lots, G.sl_usd, G.tp_usd, G.slippage, G.commission) == (
        0.09,
        5.0,
        10.0,
        0.0,
        0.0,
    )
    p, why = plan_entry(Decision(T, "sample", direction), G, Z.rules, 4000, 4000.24)
    assert why == "ready" and p.cost_floor_usd == pytest.approx(2.16)
    assert p.modeled_fill == p.requested == (4000.24 if direction == 1 else 4000.0)
    assert pnl(G, direction, p.modeled_fill, p.sl) == pytest.approx(-4.995)
    assert pnl(G, direction, p.modeled_fill, p.tp) == pytest.approx(10.008)
    assert (
        plan_entry(Decision(T, "x", direction), G, Z.rules, 4000, 4000.4)[1]
        == "spread_limit"
    )
    assert (
        plan_entry(Decision(T, "x", direction), G, Z.rules, 4000, 4000.24, 0.6)[1]
        == "stop_too_close"
    )


def test_structure_gate_holds_exit_mode_and_no_silent_resizing():
    d = Decision(T, "sweep_reclaim", 1, 1, "test", 4000, 0.1, 3998, 4003.0, 1.0)
    p, _ = plan_entry(d, G, Z.rules, 4000.05, 4000.29)
    assert (
        not p.clears_structure
        and 0 < p.max_structure_lots < 0.09
        and p.structural_risk_usd > 5
    )
    assert (
        plan_entry(
            d,
            G,
            Z.rules.model_copy(update={"require_structural_stop": True}),
            4000.05,
            4000.29,
        )[1]
        == "cash_stop_inside_structure"
    )
    r = Z.rules.model_copy(update={"exit_mode": "next_zone"})
    p, _ = plan_entry(d, G, r, 4000.05, 4000.29)
    assert p.tp == 4003
    assert reversal(replace(d, momentum=-0.9, breaks=((5, -1),)), 1, r)
    assert not reversal(replace(d, momentum=-0.9, breaks=((5, -1),)), 1, Z.rules)
    assert (
        plan_entry(replace(d, objective=4000.5), G, r, 4000.05, 4000.29)[1]
        == "opposing_zone_before_cash_target"
    )
    assert (
        plan_entry(replace(d, objective=None), G, r, 4000.05, 4000.29)[1]
        == "no_known_zone_target"
    )


def test_jpy_cash_conversion_is_not_a_gold_contract():
    s = Z.symbols["USDJPY"]
    p, _ = plan_entry(Decision(T, "x", 1), s, Z.rules, 159.20, 159.21)
    assert -5 <= pnl(s, 1, p.modeled_fill, p.sl) < -4.98
    assert 10 <= pnl(s, 1, p.modeled_fill, p.tp) < 10.02


def test_first_touch_true_tick_order_ask_side_and_bar_ambiguity():
    p = Plan(1, 100, 100, 99, 101, 0, True)
    assert hit(minute(T, [100, 102, 98, 100]), p)[2] == "target"
    assert hit(minute(T, [100, 98, 102, 100]), p)[2] == "stop"
    assert hit(minute(T, [100, 102, 98, 100], exact=False), p)[2] == "stop_ambiguous"
    assert (
        hit(minute(T, [98, 97]), p)[1] == 99
        and hit(minute(T, [98, 97]), p, "first_quote")[1] == 98
    )
    sell = Plan(-1, 100, 100, 101, 99, 0, True)
    assert (
        hit(minute(T, [100, 98.9]), sell) is None
    )  # bid crossed short TP, ask did not


def test_replay_next_open_only_not_signal_bar_and_batch_accounting(monkeypatch):
    cfg = load_yaml("config.yaml")
    cfg["zones"]["symbols"]["XAUUSD"]["legs"] = 2

    def signal(self, b):
        return Decision(b.t + 60, "event", 1 if b.t == T else 0, 1)

    monkeypatch.setattr(ZoneEngine, "update", signal)
    ms = [
        minute(T, [4000, 4005, 3990, 4000]),
        minute(T + 60, [4002, 4003.7, 3998, 4000]),
    ]
    r, trades = replay(ms, cfg, "XAUUSD")
    assert r["batches"] == 1 and len(trades) == 2
    assert all(x["entry_quote"] == 4002.24 and x["exit"] == "target" for x in trades)
    assert r["metrics"]["net_profit"] == pytest.approx(20.016)
    cfg["zones"]["limits"]["max_risk_usd"] = 9
    r, trades = replay(ms, cfg, "XAUUSD")
    assert not trades and r["blocked"] == {"portfolio_cash_risk_limit": 1}


def test_aggregate_guards_and_persistent_drawdown():
    assert portfolio_block(G, Z.limits, 1, 0.1, 12) == "portfolio_cash_risk_limit"
    assert portfolio_block(G, Z.limits, 3, 0.05, 0) == "portfolio_leg_limit"
    assert portfolio_block(G, Z.limits, 1, 0.2, 0) == "portfolio_lot_limit"
    g = EquityGuard()
    assert not g.mark("2026-11-03", 10000, Z.limits)
    assert g.mark("2026-11-03", 9965, Z.limits) == "daily_loss_halt"
    assert not g.mark("2026-11-04", 9965, Z.limits)
    assert g.mark("2026-11-04", 9900, Z.limits) == "drawdown_halt"
    assert g.mark("2026-11-05", 10001, Z.limits) == "drawdown_halt"


def test_mt5_export_spread_is_lagged_and_times_localized(tmp_path):
    p = tmp_path / "bars.csv"
    p.write_text(
        "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<SPREAD>\n2026.11.03\t14:00:00\t4000\t4001\t3999\t4000\t50\n2026.11.03\t14:01:00\t4000\t4001\t3999\t4000\t900\n"
    )
    a, b = bar_minutes(p, G, "Etc/GMT-2")
    assert a.bar.t == T
    assert a.ask[0] - a.bid[0] == pytest.approx(0.24) and b.ask[0] - b.bid[
        0
    ] == pytest.approx(0.05)


def test_tick_reader_preserves_sequence_across_files_and_rejects_crosses(tmp_path):
    files = []
    for i, rows in enumerate([[(0, 4000)], [(10, 4000.1), (60001, 3999.5)]]):
        p = tmp_path / f"{i}.csv"
        files.append(p)
        pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp(
                        T * 1000 + ms, unit="ms", tz="UTC"
                    ).isoformat(),
                    "bid": x,
                    "ask": x + 0.27,
                }
                for ms, x in rows
            ]
        ).to_csv(p, index=False)
    out = list(tick_minutes(files))
    assert len(out) == 2 and len(out[0].ms) == 2 and out[1].bar.t == T + 60
    with pytest.raises(ValueError, match="ordered"):
        list(tick_minutes(reversed(files)))
    bad = tmp_path / "bad.csv"
    bad.write_text("timestamp,bid,ask\n2026-11-03T12:00:01Z,4001,4000\n")
    with pytest.raises(ValueError, match="crossed"):
        list(tick_minutes([bad]))
