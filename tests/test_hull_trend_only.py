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
from bot.hull.structure import Structure
from bot.hull.terminal import dispatch, run

NAME = "XAUUSDm"
OK = dict(data_ok=True, history_ok=True, rewarmed=False)


def test_empty_allowlist_and_cli_override_cannot_initialize_mt5(monkeypatch):
    cfg = load_yaml(ROOT / "config.yaml")
    h = HullConfig.model_validate(cfg["hull"])
    assert cfg["run"]["mode"] == "paper" and h.eligible_timeframes == ()
    monkeypatch.setitem(
        sys.modules, "MetaTrader5", NS(initialize=lambda: pytest.fail("not allowed"))
    )
    for requested in (None, (1,), (15,)):
        with pytest.raises(ValueError, match="No eligible"):
            run(cfg, h, selection(h), frames=requested)
    approved = HullConfig.model_validate(
        cfg["hull"] | {"eligible_timeframes": ["M15", "H1"]}
    )
    assert approved.lanes() == (15, 60)
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
    for clock in ("confirmed_close_only", "live_continuation_v1"):
        with pytest.raises(ValidationError):
            HullConfig.model_validate(cfg["hull"] | {"entry_timing": clock})
    with pytest.raises(ValidationError):
        Indicator(exit_opposite=False)


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("event", [0, 1, 2, 3])
def test_trend_cross_no_midpoint_bos_or_retest_required(hull_desk, side, event):
    mt, d, events = hull_desk("medium", armed=False)
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
    p = dict(
        symbol=NAME,
        magic=d.h.magic,
        type=0 if side == 1 else 1,
        ticket=ticket,
        price_open=2000.0,
        sl=1998 if side == 1 else 2002,
        tp=2002 if side == 1 else 1998,
        volume=0.01,
        profit=0.0,
        time_msc=(T - 20) * 1000,
    )
    p.update(changes)
    pos = NS(**p)
    mt.positions.append(pos)
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


def test_conflict_still_closes_at_risk_tickets_but_never_opens_a_batch(hull_desk):
    mt, d, log = hull_desk()
    position(mt, d, -1, 98)
    a = replace(sig(), trigger=LiveTick.read(mt.q), close_s=T + 60)
    b = replace(sig(2001, -1), tf=5, trigger=a.trigger, close_s=T + 300)
    dispatch(d, NAME, [a, b], **OK)
    assert (
        len(mt.requests) == 1 and mt.requests[0]["position"] == 98 and not mt.positions
    )
    assert log[-1][1]["outcome"] == "conflicting_trend_signals_same_tick"


def test_live_repricing_vanish_and_restart_reservation(hull_desk):
    mt, d, _ = hull_desk()
    r = replace(sig(), trigger=LiveTick.read(mt.q), close_s=T + 60)
    d.verify_signal = lambda *args: False
    assert d.batch(NAME, r) == "live_cross_or_trend_no_longer_verified"
    assert not d.waiting and not mt.requests
    _, restart, _ = hull_desk(state=d.state, mt=mt)
    assert restart.batch(NAME, replace(r, trigger=None)) == "already_consumed"


def test_two_lanes_cannot_resurrect_side_flattened_at_this_tick(hull_desk):
    mt, d, log = hull_desk()
    position(mt, d, 1, 61)
    eligible = replace(sig(), trigger=LiveTick.read(mt.q), close_s=T + 60)
    exit_only = replace(raw(mt, -1), tf=5, close_s=T + 300)
    dispatch(d, NAME, [eligible, exit_only], **OK)
    assert not mt.positions and [q["position"] for q in mt.requests] == [61]
    assert log[-1][1]["outcome"] == "same_side_held_at_opposite_exit"
