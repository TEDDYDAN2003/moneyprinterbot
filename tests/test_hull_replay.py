from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from hull_cases import PATH, known_range, sequence

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
            exit_opposite=h.indicator.exit_opposite,
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
            known_range(side, 2000.0, T),
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


@pytest.mark.parametrize("bias", [1, -1])
@pytest.mark.parametrize(
    "exit_opposite,valid", [(False, False), (False, True), (True, False), (True, True)]
)
def test_replay_original_opposite_exit_separate_from_structure_entry(
    bias, exit_opposite, valid
):
    cfg = load_yaml(Path(__file__).resolve().parents[1] / "config.yaml")
    h = HullConfig.model_validate(cfg["hull"])
    g = Grid(0.001, 0.001, 3, 0, 0.01, 100, 0.01)
    p = h.symbols["XAUUSD"].model_copy(update={"lots": 0.01})
    a = Account(
        p,
        h.limits,
        g,
        lambda d, l, e, x: d * l * 100 * (x - e),
        Hours(cfg),
        10000,
        exit_opposite=exit_opposite,
    )
    r = sequence(bias, closes=PATH[:21], end_s=T + 60)[-1]
    # At constant prices neither attached barrier is hit. Seed the held side,
    # then feed the genuine, opposite post-BOS label or its equality case.
    x = r.close

    def minute(t):
        return Minute(
            Bar(t, x, x, x, x),
            np.array([t * 1000 + 10]),
            np.array([x]),
            np.array([x + 0.1]),
            True,
        )

    first = replace(
        r,
        close_s=T,
        side=bias,
        label=bias,
        raw_sl=x - bias,
        structure=known_range(bias, x, T),
    )
    a.step(minute(T), [first])
    assert a.pos is not None and a.pos[1].side == bias
    next_r = r if valid else replace(r, close=r.structure.broken.price)
    a.step(minute(T + 60), [next_r])
    assert sum(a.labels.values()) == 2 and sum(a.qualified.values()) == 1 + int(valid)
    if exit_opposite:
        assert len(a.rows) == 1 and a.rows[0]["exit_reason"] == "opposite_hull_label"
        assert a.rows[0]["exit_price"] == (x if bias == 1 else x + 0.1)
        assert (a.pos[1].side if a.pos else 0) == (-bias if valid else 0)
    else:
        assert not a.rows and a.pos[1].side == bias
