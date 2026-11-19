"""Stop/target barrier priority and per-ticket cash locks on ordered quotes, not OHLC."""

from dataclasses import replace

import numpy as np
import pytest
from hull_mt import ROOT, T, sig

from bot.config.loader import load_yaml
from bot.hull.config import FIXED, LADDER, HullConfig
from bot.hull.orders import Grid
from bot.hull.replay import Account
from bot.zones.engine import Bar
from bot.zones.feed import Minute
from bot.zones.hours import Hours


def tape(side, pnls, policy=LADDER):
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["hull"]["limits"].update(
        drawdown_usd=75, max_orders_minute=40
    )  # old full-file config
    h = HullConfig.model_validate(cfg["hull"])
    assert h.exit_policy == LADDER
    p = h.symbols["XAUUSD"].model_copy(update={"rate": "medium", "lots": 0.10})
    cash = lambda d, l, e, px: d * l * 100 * (px - e)
    a = Account(
        p,
        h.limits,
        Grid(0.001, 0.001, 3, 0, 0.01, 100, 0.01),
        cash,
        Hours(cfg),
        10000,
        exit_opposite=True,
        exit_policy=policy,
    )
    entry = 2000.1 if side == 1 else 2000.0
    marks = np.array([round(entry + side * usd / 10, 3) for usd in pnls])
    bid = marks if side == 1 else marks - 0.1
    ask = marks + 0.1 if side == 1 else marks
    # The entry signal opens on the first actual tick (spread costs $1 per ticket).
    m = Minute(
        Bar(T, bid[0], max(bid), min(bid), bid[-1]),
        np.arange(len(marks)) + T * 1000,
        bid,
        ask,
        True,
    )
    a.step(m, [replace(sig(side=side), raw_sl=entry - side * 0.6)])
    return a, m


@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize(
    "path,rung,why,usd",
    [
        ([-1, 6.99, 7, 10, 13, 16, 15], 4, "ladder_stop", 15),
        ([-1, 7, 6], 1, "ladder_stop", 6),
        ([-1, 7, 10, 9.5, 9], 2, "ladder_stop", 9),
        ([-1, 7, 10, 9.8, 13, 12], 3, "ladder_stop", 12),
        (
            [-1, 7, 3],
            1,
            "ladder_stop",
            3,
        ),  # gaps fill at crossing quote, not hypothetical $6
        ([-1, 6.99, 13], 0, "target", 10),  # old hard TP hit before we could move it
        ([-1, 7, 14], 1, "target", 12),  # gap through the first extended TP
    ],
)
def test_ordered_ticks_cannot_backdate_higher_stops_or_rescue_old_tp(
    side, path, rung, why, usd
):
    a, m = tape(side, path)
    assert not a.pos and len(a.rows) == 1
    (row,) = a.rows
    assert (row["ladder_rung"], row["exit_reason"], row["legs"]) == (rung, why, 4)
    assert row["net_usd_per_leg"] == pytest.approx(usd)
    metrics = a.finish(m)
    assert metrics["ladder_ticket_amendments"] == 4 * rung and metrics["batches"] == 1
    assert metrics["net_profit"] == pytest.approx(4 * usd)


@pytest.mark.parametrize("side", [1, -1])
def test_replay_all_tickets_can_advance_past_old_shared_send_cap(side):
    # Four entries + 44 amendments in 12 ms; return from +$37 to the +$36 stop.
    a, m = tape(side, [-1, *range(7, 38, 3), 36])
    assert not a.pos
    (row,) = a.rows
    assert (row["legs"], row["ladder_rung"], row["exit_reason"]) == (
        4,
        11,
        "ladder_stop",
    )
    metrics = a.finish(m)
    assert metrics["batches"] == 1 and metrics["ladder_ticket_amendments"] == 44
    assert metrics["net_profit"] == pytest.approx(4 * 36)


@pytest.mark.parametrize("invalid", [False, True])
def test_replay_can_reenter_after_100_dollar_loss_but_not_with_invalid_equity(invalid):
    a, m = tape(1, [-1, -25])  # four tickets stopped through a gap
    assert not a.pos and a.eq == pytest.approx(9900)
    if invalid:
        a.eq = 0.0
    t = T + 60
    next_minute = Minute(
        Bar(t, 2000.0, 2000.0, 2000.0, 2000.0),
        m.ms[:1] + 60000,
        m.bid[:1],
        m.ask[:1],
        True,
    )
    a.step(next_minute, [replace(sig(), close_s=t)])
    if invalid:
        assert not a.pos and a.count["invalid_equity"] == 1
    else:
        assert a.pos and a.pos[2] == 4 and a.count["opened"] == 2


def test_explicit_fixed_policy_keeps_original_replay_behavior():
    a, m = tape(1, [-1, 7, 10, 13, 12], policy=FIXED)
    assert a.rows[0]["ladder_rung"] == 0 and a.rows[0]["exit_reason"] == "target"
    assert a.finish(m)["ladder_ticket_amendments"] == 0
