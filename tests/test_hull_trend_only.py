"""Bot policy tests: raw exit and trend-gated entry are deliberately different decisions."""

import sys
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_mt import ROOT, T, sig
from pydantic import ValidationError

from bot.config.loader import load_yaml
from bot.hull.config import HullConfig, Indicator, selection
from bot.hull.curves import LiveTick
from bot.hull.signal import Hull
from bot.hull.structure import Structure
from bot.hull.terminal import dispatch, run

NAME = "XAUUSDm"
OK = {"data_ok": True, "history_ok": True, "rewarmed": False}


def test_shipped_and_missing_length_both_drive_a_and_b_from_16():
    raw = load_yaml(ROOT / "config.yaml")["hull"]
    assert raw["indicator"]["hull_len"] == 16
    for settings in (raw["indicator"], {}, {"source": "dynamic"}):
        cfg = HullConfig.model_validate(raw | {"indicator": settings})
        assert cfg.indicator.hull_len == 16
        engine = Hull(15, cfg.indicator, 0.01)
        assert {
            source: (a.fast.n, a.slow.n, a.smooth.n)
            for source, a in engine.baselines.items()
        } == {"low": (8, 16, 4), "high": (8, 16, 4)}
        assert tuple(w.n for w in engine.b_weights) == (2, 4, 8, 8)
    without_indicator = {k: v for k, v in raw.items() if k != "indicator"}
    assert HullConfig.model_validate(without_indicator).indicator.hull_len == 16


@pytest.mark.parametrize("period", [24, 12])
def test_active_bot_refuses_non_16_private_settings(period):
    raw = load_yaml(ROOT / "config.yaml")["hull"]
    with pytest.raises(ValidationError, match=r"hull\.indicator\.hull_len: 16"):
        HullConfig.model_validate(
            raw | {"indicator": raw["indicator"] | {"hull_len": period}}
        )


def test_empty_allowlist_and_cli_override_cannot_initialize_mt5(monkeypatch):
    cfg = load_yaml(ROOT / "config.yaml")
    h = HullConfig.model_validate(cfg["hull"])
    assert cfg["run"]["mode"] == "paper" and h.lanes() == (15, 30, 45)
    h = HullConfig.model_validate(cfg["hull"] | {"eligible_timeframes": []})
    monkeypatch.setitem(
        sys.modules, "MetaTrader5", NS(initialize=lambda: pytest.fail("not allowed"))
    )
    for requested in (None, (1,), (15,)):
        with pytest.raises(ValueError, match="No eligible"):
            run(cfg, h, selection(h), frames=requested)
    approved = HullConfig.model_validate(
        cfg["hull"] | {"eligible_timeframes": ["M15", "M45"]}
    )
    assert approved.lanes() == (15, 45)
    with pytest.raises(ValueError, match="not in"):
        approved.lanes((1,))
    with pytest.raises(ValidationError):
        HullConfig.model_validate(
            cfg["hull"]
            | {
                "eligible_timeframes": ["M5"],
                "indicator": {"structure_timeframe": "M2"},
            }
        )
    for clock in ("live_hull_cross", "confirmed_close_only", "live_continuation_v1"):
        with pytest.raises(ValidationError):
            HullConfig.model_validate(cfg["hull"] | {"entry_timing": clock})
    with pytest.raises(ValidationError):
        Indicator(exit_opposite=False)


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("event", [0, 1, 3])
def test_trend_cross_no_midpoint_bos_or_retest_required(hull_desk, side, event):
    mt, d, _events = hull_desk("medium", armed=False)
    # No range/extreme exists. Price need not visit a discount/premium half at signal or quote.
    r = replace(
        sig(1999 if side == 1 else 2001, side),
        structure=Structure(T, side, event * side),
    )
    assert r.entry_side == side and d.batch(NAME, r) == "shadow_plan_only"
    assert "range_midpoint" not in r.entry_audit() and not mt.requests
    assert (
        replace(r, label=0).entry_side == 0
    )  # BOS/MSS without a Hull cross is not a trade
    assert replace(r, structure=Structure(T, 0)).entry_reason == "neutral_structure"
    assert (
        replace(r, structure=Structure(T, -side)).entry_reason
        == "opposite_signal_exit_only"
    )


def position(mt, d, side, ticket, **changes):
    p = {
        "symbol": NAME,
        "magic": d.h.magic,
        "type": 0 if side == 1 else 1,
        "ticket": ticket,
        "price_open": 2000.0,
        "sl": 1998 if side == 1 else 2002,
        "tp": 2002 if side == 1 else 1998,
        "volume": 0.01,
        "profit": 0.0,
        "time_msc": (T - 20) * 1000,
    }
    p.update(changes)
    pos = NS(**p)
    mt.positions.append(pos)
    if d.owned(pos):
        assert d.owners.bind(pos, 1)
    return pos


def raw(mt, side):
    # No usable stop and NO eligibility to enter. Still an exit at the live tick.
    return replace(
        sig(side=side),
        label=side,
        side=0,
        raw_sl=None,
        reason="no_known_swing_or_atr",
        structure=Structure(T, -side),
        trigger=LiveTick.read(mt.q),
        close_s=T + 60,
    )


@pytest.mark.parametrize("side", [-1, 1])
def test_opposite_raw_live_exit_first_even_if_no_stop_history_or_entry_trend(
    hull_desk, side
):
    mt, d, events = hull_desk()
    r = raw(mt, side)
    for t in (51, 52):
        position(mt, d, -side, t)
    safe = position(mt, d, side, 53)
    manual = position(mt, d, -side, 54, magic=0)
    unrelated = position(mt, d, -side, 55, symbol="GBPUSDm")
    newer = position(mt, d, -side, 56, time_msc=r.signal_msc + 1)
    dispatch(d, NAME, [r], data_ok=False, history_ok=False, rewarmed=False)
    assert [p["position"] for p in mt.requests] == [51, 52]
    assert mt.positions == [safe, manual, unrelated, newer] and not d.exits
    assert not any(e == "batch_preview" for e, _ in events)


def test_exit_latches_until_new_quote_and_never_blindly_resends(hull_desk):
    mt, d, _ = hull_desk()
    position(mt, d, 1, 73)
    r = raw(mt, -1)
    mt.q.time_msc = (T - 10) * 1000
    dispatch(d, NAME, [r], **OK)
    assert not mt.requests and d.exits["73"]["status"] == "await_quote"
    mt.q.time_msc = T * 1000 + 150
    mt.bad = True
    d.supervise()
    assert len(mt.requests) == 1 and d.exits["73"]["status"] == "verify_or_review"
    d.supervise()
    assert len(mt.requests) == 1 and mt.positions
    assert d.batch(NAME, sig()) == "opposite_exit_not_confirmed_flat"
    mt.positions.clear()
    d.supervise()
    assert not d.exits


def test_simultaneous_opposite_entries_remain_independent(hull_desk):
    mt, d, _log = hull_desk("medium")
    position(mt, d, -1, 98)
    d.verify_signal = lambda *_: True
    a = sig()
    b = replace(sig(2001, -1), tf=5)
    dispatch(d, NAME, [a, b], **OK)
    assert mt.requests[0]["position"] == 98
    assert (
        sorted((d.owners.lane(p), p.type) for p in mt.positions)
        == [(1, 0)] * 4 + [(5, 1)] * 4
    )


def test_no_live_reservation_but_rejected_confirmed_entry_stays_consumed_on_restart(
    hull_desk, monkeypatch
):
    mt, d, _ = hull_desk()
    r = replace(sig(), trigger=LiveTick.read(mt.q), close_s=T + 60)
    d.verify_signal = lambda *_: False
    assert d.batch(NAME, r) == "await_cross_candle_close" and not d.state.get(
        "dynamic_entry_bars"
    )
    monkeypatch.setattr("bot.hull.desk.time.time", lambda: T + 60.2)
    mt.q.time_msc = (T + 60) * 1000 + 100
    closed = replace(r, trigger=None)
    assert (
        d.batch(NAME, closed) == "next_bar_quote_or_context_unverified"
        and not mt.requests
    )
    _, restart, _ = hull_desk(state=d.state, mt=mt)
    assert restart.batch(NAME, closed) == "already_consumed" and not restart.waiting


def test_sibling_raw_exit_cannot_touch_occupied_lane(hull_desk):
    mt, d, log = hull_desk()
    position(mt, d, 1, 61)
    eligible = sig()
    exit_only = replace(raw(mt, -1), tf=5, close_s=T + 300)
    dispatch(d, NAME, [eligible, exit_only], **OK)
    assert [p.ticket for p in mt.positions] == [61] and not mt.requests
    assert any(
        e == "label_decision"
        and x["tf"] == 1
        and x["outcome"] == "same_side_batch_open"
        for e, x in log
    )
