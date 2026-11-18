from datetime import UTC, datetime
from types import SimpleNamespace as NS

import numpy as np
import pytest

from bot.config.loader import load_yaml
from bot.execution.demo_runner import DemoSafetyError
from bot.zones.config import ZoneConfig
from bot.zones.engine import Decision
from bot.zones.export import export
from bot.zones.feed import tick_minutes
from bot.zones.monitor import monitor
from bot.zones.price_action import arm
from bot.zones.quote_path import path_cancel, quotes_to_snapshot
from bot.zones.watch_monitor import SymbolWatch

T = int(datetime(2026, 11, 3, 12, tzinfo=UTC).timestamp())
Z = ZoneConfig.model_validate(load_yaml("config.yaml")["zones"])
DT = [("time_msc", "i8"), ("bid", "f8"), ("ask", "f8")]


class Feed:
    COPY_TICKS_ALL = 42

    def __init__(self, rows):
        self.data = np.array(rows, dtype=DT)
        self.calls = 0

    def symbol_select(self, *args):
        return True

    def symbol_info(self, *args):
        return NS(
            point=0.001,
            trade_tick_size=0.001,
            trade_contract_size=100,
            currency_profit="USD",
            trade_stops_level=0,
            trade_freeze_level=0,
        )

    def copy_ticks_range(self, name, lo, hi, kind):
        self.calls += 1
        m = self.data["time_msc"]
        return self.data[
            (m >= int(lo.timestamp()) * 1000) & (m <= int(hi.timestamp()) * 1000)
        ]

    def symbol_info_tick(self, name):
        r = self.data[-1]
        return NS(time_msc=int(r["time_msc"]), bid=float(r["bid"]), ask=float(r["ask"]))


def signal():
    return Decision(
        T,
        "sweep_reclaim",
        -1,
        5,
        "bsl",
        4000,
        0.2,
        4000.6,
        3995,
        1.2,
        candle=("bear_engulf",),
        momentum=-0.75,
        bar_direction=-1,
        trigger_high=4000.6,
        trigger_low=3999.7,
    )


def test_exact_broker_export_keeps_same_ms_and_half_open_hour_boundary(tmp_path):
    rows = [
        (T * 1000 + 5, 4000, 4000.24),
        ((T + 3600) * 1000, 3999, 3999.24),
        ((T + 3600) * 1000, 3999.1, 3999.3),
        ((T + 7200) * 1000, 4001, 4001.2),
    ]
    mt = Feed(rows)
    out = tmp_path / "gold.csv.gz"
    r = export(mt, "XAUUSDm", "2026-11-03T12:00:00Z", "2026-11-03T14:00:00Z", out)
    assert mt.calls == 2 and r["ticks"] == 3 and r["broker_symbol"] == "XAUUSDm"
    values = list(tick_minutes([out]))
    assert len(values) == 2 and len(values[1].ms) == 2
    assert out.with_name(out.name + ".manifest.json").exists()
    with pytest.raises(FileExistsError):
        export(mt, "X", "2026-11-03T12:00Z", "2026-11-03T14:00Z", out)


def test_no_bad_or_empty_export_is_published(tmp_path):
    p = tmp_path / "bad.csv"
    mt = Feed([(T * 1000, 4001.0, 4000.0)])
    with pytest.raises(ValueError, match="Malformed"):
        export(mt, "x", "2026-11-03T12:00Z", "2026-11-03T13:00Z", p)
    assert not p.exists() and not list(tmp_path.glob("*.partial"))
    with pytest.raises(ValueError, match="No historical"):
        export(mt, "x", "2026-10-01", "2026-10-02", p)


def test_no_historical_or_stale_shadow_fill_after_hidden_break(monkeypatch):
    rows = [
        (T * 1000, 3999.7, 3999.94),
        (T * 1000 + 100, 4000.601, 4000.841),
        (T * 1000 + 2000, 4000.53, 4000.77),
    ]
    mt = Feed(rows)
    q = mt.symbol_info_tick("X")
    w, _ = arm(signal(), Z.symbols["XAUUSD"], Z.price_action)
    assert (
        path_cancel(w, quotes_to_snapshot(mt, "X", T * 1000, q)[1])
        == "invalidated_before_entry"
    )
    assert (
        quotes_to_snapshot(
            mt, "X", T * 1000, NS(time_msc=q.time_msc, bid=3999, ask=3999.2)
        )
        is None
    )
    events = []
    desk = NS(mt=mt, max_age=5, lim=Z.limits, emit=lambda a, b: events.append((a, b)))
    c = SymbolWatch(Z.symbols["XAUUSD"], Z)
    monkeypatch.setattr("bot.zones.watch_monitor.time.time", lambda: T + 2.1)
    c.poll(desk, signal(), False, False, True, [], False, "", False, False)
    assert c.w is None and c.paper is None
    assert events[-1][1]["reason"] == "invalidated_before_entry"


def test_shadow_holds_then_exits_on_observed_barrier_between_polls(monkeypatch):
    t = T * 1000
    mt = Feed([(t, 3999.7, 3999.94), (t + 2000, 4000.53, 4000.77)])
    now = [T + 2.2]
    events = []
    desk = NS(mt=mt, max_age=5, lim=Z.limits, emit=lambda a, b: events.append((a, b)))
    c = SymbolWatch(Z.symbols["XAUUSD"], Z)
    monkeypatch.setattr("bot.zones.watch_monitor.time.time", lambda: now[0])
    c.poll(desk, signal(), False, False, True, [], False, "", False, False)
    assert c.paper and events[-1][1]["outcome"] == "shadow_open"
    # Ask hits fixed attached SL then falls back; a snapshot-only implementation would miss the stop.
    mt.data = np.array(
        [
            (t + 2000, 4000.53, 4000.77),
            (t + 2100, 4000.58, 4000.90),
            (t + 4000, 4000.49, 4000.73),
        ],
        dtype=DT,
    )
    now[0] = T + 4.2
    c.poll(desk, None, False, False, True, [], False, "", False, False)
    assert (
        not c.paper
        and events[-1][1]["reason"] == "stop"
        and -5 <= events[-1][1]["net_usd_per_leg"] < 0
    )


def test_challenger_demo_requires_explicit_optin_even_in_demo_mode():
    cfg = load_yaml("config.yaml")
    cfg["run"]["mode"] = "demo"
    with pytest.raises(DemoSafetyError, match="shadow-only"):
        monitor(cfg, armed=True)


def test_partial_price_history_without_left_edge_cannot_verify_pending_setup():
    mt = Feed([(T * 1000 + 100, 4000.0, 4000.24)])
    assert quotes_to_snapshot(mt, "X", T * 1000, mt.symbol_info_tick("X")) is None


def test_new_bar_not_required_to_open_shadow_return(monkeypatch):
    t = T * 1000
    mt = Feed([(t, 3999.7, 3999.94)])
    now = [T + 0.1]
    events = []
    desk = NS(mt=mt, max_age=5, lim=Z.limits, emit=lambda a, b: events.append((a, b)))
    c = SymbolWatch(Z.symbols["XAUUSD"], Z)
    monkeypatch.setattr("bot.zones.watch_monitor.time.time", lambda: now[0])
    c.poll(desk, signal(), False, False, True, [], False, "", False, False)
    assert c.w and not c.paper and events[-1][1]["reason"] == "outside_swept_pocket"
    mt.data = np.array([(t, 3999.7, 3999.94), (t + 2200, 4000.53, 4000.77)], dtype=DT)
    now[0] = T + 2.3
    c.poll(desk, None, False, False, True, [], False, "", False, False)
    assert not c.w and c.paper and events[-1][1]["outcome"] == "shadow_open"
    # Identical millisecond, new quote crosses, then returns to the identical price.
    mt.data = np.array(
        [
            (t + 2200, 4000.53, 4000.77),
            (t + 2200, 4000.54, 4000.88),
            (t + 2200, 4000.53, 4000.77),
        ],
        dtype=DT,
    )
    c.poll(desk, None, False, False, True, [], False, "", False, False)
    assert not c.paper and events[-1][1]["reason"] == "stop"
