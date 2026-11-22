"""Current first-eligible-while-flat scanner and deal-history regressions."""

from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from hull_mt import ROOT, T, sig

from bot.config.loader import load_yaml
from bot.hull.config import BATCH_ENTRY_POLICY, LADDER, HullConfig, Indicator
from bot.hull.terminal import dispatch

NAME = "XAUUSDm"
OK = {"data_ok": True, "history_ok": True, "rewarmed": False}


@pytest.mark.parametrize("side", [-1, 1])
def test_live_one_remaining_leg_consumes_repeat_then_same_zone_rearms_only_on_fresh_cross(
    hull_desk, monkeypatch, side
):
    mt, d, logs = hull_desk("slow")
    r = sig(sl=1999.95 if side == 1 else 2000.55, side=side)
    now = [T + 0.3]
    monkeypatch.setattr("bot.hull.desk.time.time", lambda: now[0])
    dispatch(d, NAME, [r], **OK)
    assert len(mt.positions) == len(mt.requests) == 10
    # Nine exits are not a flat batch. No modify, exit, wait, new order or top-up.
    mt.positions[:] = mt.positions[-1:]
    survivor = mt.positions[0]
    levels = (survivor.sl, survivor.tp)
    again = replace(r, close_s=T + 60)
    now[0] += 60
    mt.q.time_msc = (T + 60) * 1000 - 10
    dispatch(d, NAME, [again], **OK)
    assert logs[-1][1]["outcome"] == "same_side_batch_open"
    assert (
        mt.positions == [survivor]
        and (survivor.sl, survivor.tp) == levels
        and len(mt.requests) == 10
        and not d.waiting
    )
    mt.positions.clear()
    mt.q.time_msc += 110
    dispatch(d, NAME, [again], **OK)
    assert logs[-1][1]["outcome"] == "already_consumed" and not mt.positions
    now[0] += 60
    mt.q.time_msc += 60000
    dispatch(d, NAME, [replace(r, close_s=T + 120)], **OK)
    assert (
        logs[-1][1]["outcome"] == "demo_batch_sent"
        and len(mt.positions) == 10
        and len(mt.requests) == 20
    )


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize(
    "offset,entry,deal_side,expected",
    [
        (-1, 1, "closing", "shadow_plan_only"),
        (0, 1, "closing", "same_side_batch_at_signal_close"),
        (150, 1, "closing", "same_side_batch_at_signal_close"),
        (150, 3, "closing", "same_side_batch_at_signal_close"),
        (150, 2, "closing", "same_side_batch_at_signal_close"),
        (150, 0, "closing", "shadow_plan_only"),
        (150, 1, "reverse", "shadow_plan_only"),
    ],
)
def test_broker_exit_between_cross_and_poll_not_resurrected_or_confused_with_reversal(
    hull_desk, side, offset, entry, deal_side, expected
):
    mt, d, _ = hull_desk(armed=False)
    close_type = mt.DEAL_TYPE_SELL if side == 1 else mt.DEAL_TYPE_BUY
    mt.deals = [
        NS(
            symbol=NAME,
            entry=entry,
            type=close_type if deal_side == "closing" else 1 - close_type,
            magic=0,
            time_msc=T * 1000 + offset,
        )
    ]
    r = sig(1999.95 if side == 1 else 2000.55, side=side)
    assert d.batch(NAME, r) == expected and not mt.requests
    mt.deals = []
    assert d.batch(NAME, r) == "already_consumed"


@pytest.mark.parametrize("bad", [None, "missing_time"])
def test_flat_history_uncertain_is_hard_veto_not_a_queue(hull_desk, bad):
    mt, d, _ = hull_desk()
    mt.deals = (
        None
        if bad is None
        else [NS(symbol=NAME, entry=1, type=mt.DEAL_TYPE_SELL, time_msc=0)]
    )
    assert (
        d.batch(NAME, sig()) == "entry_deal_history_unavailable"
        and not d.waiting
        and not mt.requests
    )


def test_versioned_defaults_only_indicator_input_changes_and_legacy_opt_in():
    h = HullConfig.model_validate(load_yaml(ROOT / "config.yaml")["hull"])
    p = h.indicator
    assert p == Indicator() and (p.hull_len, p.source, p.signal_policy) == (
        16,
        "dynamic",
        "fc_dynamic_source_internal_v1",
    )
    assert (
        p.exit_opposite,
        p.stop,
        p.pullback_bars,
        p.atr_length,
        p.atr_buffer,
        p.extra_pips,
        h.exit_policy,
    ) == (True, "cross_candle", 7, 14, 0.1, 0, LADDER)
    assert BATCH_ENTRY_POLICY == "first_eligible_while_lane_flat_v2"
    fixed = Indicator(source="hl2")
    assert (
        fixed.hull_len == 16 and fixed.signal_policy == "fc_dynamic_source_internal_v1"
    )


def test_a_waiting_quote_cannot_revive_a_repeat_when_history_catches_up(hull_desk):
    from bot.hull.entry_clock import WAITING

    mt, desk, _ = hull_desk()
    quote = mt.q
    mt.q = None
    assert desk.batch(NAME, sig()) == WAITING
    mt.deals = [
        NS(symbol=NAME, entry=1, type=mt.DEAL_TYPE_SELL, time_msc=T * 1000 + 50)
    ]
    mt.q = quote
    assert desk.resume_first_quote(NAME, 1) == "same_side_batch_at_signal_close"
    assert not desk.waiting and not mt.requests
    assert desk.batch(NAME, sig()) == "already_consumed"


def test_other_symbols_closing_or_deposit_deals_do_not_lock_this_first_entry(hull_desk):
    mt, desk, _ = hull_desk(armed=False)
    mt.deals = [
        NS(symbol="GBPJPYm", entry=1, type=mt.DEAL_TYPE_SELL, time_msc=T * 1000 + 50),
        NS(symbol="", entry=0, type=2, time_msc=T * 1000 + 80),
    ]
    assert desk.batch(NAME, sig()) == "shadow_plan_only" and not mt.requests
