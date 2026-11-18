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


def tape(side, pnls, cap=40, policy=LADDER):
    cfg = load_yaml(ROOT / "config.yaml")
    h = HullConfig.model_validate(cfg["hull"])
    assert h.exit_policy == LADDER
    p = h.symbols["XAUUSD"].model_copy(update={"rate": "medium", "lots": 0.10})
    cash = lambda d, l, e, px: d * l * 100 * (px - e)
    a = Account(
        p,
        h.limits.model_copy(update={"max_orders_minute": cap}),
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
def test_limited_amendments_split_realized_groups_not_batch_count(side):
    # Four entries, two remaining amendment requests. Other two legs retain initial TP.
    a, m = tape(side, [-1, 7, 10, 6.5, 6], 6)
    assert a.pos is None
    rows = sorted(
        (r["legs"], r["ladder_rung"], r["exit_reason"], r["net_usd_per_leg"])
        for r in a.rows
    )
    assert rows == [(2, 0, "target", 10.0), (2, 1, "ladder_stop", 6.0)]
    metrics = a.finish(m)
    assert (
        metrics["batches"] == 1
        and metrics["positions"] == 4
        and metrics["closed_groups"] == 2
    )
    assert metrics["ladder_ticket_amendments"] == 2 and metrics[
        "net_profit"
    ] == pytest.approx(32.0)


def test_explicit_fixed_policy_keeps_original_replay_behavior():
    a, m = tape(1, [-1, 7, 10, 13, 12], policy=FIXED)
    assert a.rows[0]["ladder_rung"] == 0 and a.rows[0]["exit_reason"] == "target"
    assert a.finish(m)["ladder_ticket_amendments"] == 0
