from pathlib import Path

import numpy as np

from bot.config.loader import load_yaml
from bot.hull.config import HullConfig, Indicator
from bot.hull.orders import Grid
from bot.hull.replay import Account, Clock
from bot.hull.signal import Reading
from bot.zones.engine import Bar
from bot.zones.feed import Minute
from bot.zones.hours import Hours

T = 1794312000


def test_m2_aggregation_never_reads_the_forming_bar_to_confirm_signal():
    c = Clock(2, Indicator(), 0.01)
    a = Bar(T, 100, 102, 99, 101)
    b = Bar(T + 60, 101, 104, 98, 100.5)
    assert c.before(a) is None and c.before(b) is None
    r = c.before(Bar(T + 120, 500, 800, 400, 450))
    assert (
        r.close_s == T + 120
        and r.close == 100.5
        and c.h.prev == Bar(T, 100, 104, 98, 100.5)
    )


def test_first_crossing_quote_gap_stop_and_tp_never_waits_for_momentum():
    cfg = load_yaml(Path(__file__).resolve().parents[1] / "config.yaml")
    h = HullConfig.model_validate(cfg["hull"])
    p = h.symbols["XAUUSD"]
    g = Grid(0.001, 0.001, 3, 0, 0.01, 100, 0.01)

    def go(next_bid, side):
        a = Account(
            p,
            h.limits,
            g,
            lambda side, l, e, px: side * l * 100 * (px - e),
            Hours(cfg),
            10000,
        )
        bid = np.array([2000.0, next_bid])
        ask = bid + 0.1
        m = Minute(
            Bar(T, 2000.0, max(bid), min(bid), bid[-1]),
            np.array([T * 1000 + 20, T * 1000 + 21]),
            bid,
            ask,
            True,
        )
        sig = Reading(
            T,
            1,
            2000.0,
            1999.0,
            2000.0,
            2000.0,
            0.5,
            side,
            side,
            "ready",
            1999.75 if side == 1 else 2000.4,
            T - 120,
            1999.7 if side == 1 else 2000.45,
            T - 240,
            4,
            None,
            None,
        )
        a.step(m, [sig])
        return a.rows[0]

    for side in (-1, 1):
        win = go(2000 + side * 3, side)
        assert win["exit_reason"] == "target" and win["net_usd_per_leg"] >= 10
        loss = go(2000 - side * 2, side)
        assert (
            loss["exit_reason"] == "stop"
            and -loss["net_usd_batch"] > loss["risk_usd_batch"]
        )
