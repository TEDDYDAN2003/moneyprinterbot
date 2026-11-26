"""Direct (one move) pullbacks. All distances are prices on the broker's trade grid."""

from __future__ import annotations

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from .config import STOP_SPREAD_POLICY
from .orders import (
    Block,
    Plan,
    cross_candle_stop,
    protected_prices,
    spread_goal,
    target,
    valid_cash,
)

POLICY = "confirmed_pair_direct_retest_v1"
GOAL = "base_plus_placement_spread_v1"


def capability(mt, info):
    # SYMBOL_ORDER_LIMIT|SL|TP, SYMBOL_EXPIRATION_SPECIFIED (MetaQuotes flags).
    # Some MT5 Python builds don't export the symbolic bit constants. Never
    # assume capability from their absence: the broker's actual masks are required.
    modes = getattr(info, "order_mode", 0) or 0
    flags = sum(
        getattr(mt, k, v)
        for k, v in [
            ("SYMBOL_ORDER_LIMIT", 2),
            ("SYMBOL_ORDER_SL", 16),
            ("SYMBOL_ORDER_TP", 32),
        ]
    )
    if (modes & flags) != flags:
        return "limit_or_attached_brackets_unsupported"
    if not (getattr(info, "expiration_mode", 0) or 0) & getattr(
        mt, "SYMBOL_EXPIRATION_SPECIFIED", 4
    ):
        return "server_specified_expiration_unsupported"
    if any(
        not hasattr(mt, k)
        for k in (
            "TRADE_ACTION_PENDING",
            "TRADE_ACTION_REMOVE",
            "ORDER_TYPE_BUY_LIMIT",
            "ORDER_TYPE_SELL_LIMIT",
            "ORDER_TIME_SPECIFIED",
            "ORDER_FILLING_RETURN",
        )
    ):
        return "pending_api_unavailable"
    return ""


def level(bar, side, atr, tick, fraction, cap):
    if (
        side not in (-1, 1)
        or not all(
            isinstance(v, (float, int)) and math.isfinite(v) and v > 0
            for v in (atr, tick, fraction, cap)
        )
        or max(fraction, cap) > 1
    ):
        raise Block("invalid_retest_price_inputs")

    def D(v):
        return Decimal(str(v))

    o, c, hi, lo, t = map(D, (bar.o, bar.c, bar.h, bar.l, tick))
    body = side * (c - o) > 0
    span = abs(c - o) if body else c - lo if side == 1 else hi - c
    ceiling = min(span, D(atr) * D(cap))
    if ceiling < t:
        raise Block("no_one_tick_pullback_inside_candle_and_atr_cap")
    a, b = sorted((c - side * t, c - side * ceiling))
    low = (a / t).to_integral_value(rounding=ROUND_CEILING)
    high = (b / t).to_integral_value(rounding=ROUND_FLOOR)
    if low > high:
        raise Block("no_tradable_retest_tick")
    want = c - side * max(t, min(ceiling, span * D(fraction)))
    index = max(low, min(high, (want / t).to_integral_value(rounding=ROUND_HALF_UP)))
    segment = "body" if body else "lower_wick" if side == 1 else "upper_wick"
    return float(index * t), segment


def placement_check(side, entry, sl, tp, grid, bid, ask, spread_cap):
    if not (
        0 < bid <= ask and all(math.isfinite(v) for v in (bid, ask, entry, sl, tp))
    ):
        raise Block("invalid_quote")
    tol = grid.tick * 1e-7
    gap = max(grid.tick, grid.gap)
    if spread_cap is not None and ask - bid > spread_cap + tol:
        raise Block("spread_cap")
    touch = ask if side == 1 else bid
    if min(sl, tp) <= 0 or min(side * (entry - sl), side * (tp - entry)) < gap - tol:
        raise Block("retest_brackets_too_close")
    if side * (touch - entry) <= tol:
        raise Block("retest_already_touched")
    if side * (touch - entry) < gap - tol:
        raise Block("retest_placement_gap")


def make_retest_plan(r, p, g, bid, ask, cash, h):
    b = r.cross_bar
    if (
        not r.paired
        or r.trigger is not None
        or b is None
        or b.t != r.anchor_s
        or b.t + r.candle_tf * 60 != r.close_s
        or b.c != r.close
        or (b.l if r.side == 1 else b.h) != r.anchor
        or r.stop_tick_size != g.tick
        or r.atr is None
    ):
        raise Block("retest_trigger_ohlc_unverified")
    pad = h.indicator.atr_buffer * r.atr + h.indicator.extra_pips * p.pip_size
    if r.raw_sl != cross_candle_stop(r.anchor, r.side, pad, g.tick):
        raise Block("retest_stop_unverified")
    _, _, sl, _, raw_sl, stop_spread_buffer = protected_prices(r, p, g, bid, ask)
    entry, _ = level(b, r.side, r.atr, g.tick, h.retest_fraction, h.retest_atr_cap)
    risk = -valid_cash(cash, r.side, p.lots, entry, sl)
    if risk <= 0:
        raise Block("nonpositive_cash_risk")
    spread, goal = spread_goal(r.side, p.lots, bid, ask, p.target_usd, cash)
    tp, reward = target(
        g, r.side, p.lots, entry, goal, cash, cash_per_price=risk / abs(entry - sl)
    )
    placement_check(r.side, entry, sl, tp, g, bid, ask, p.max_spread)
    return Plan(
        r.side,
        p.lots,
        entry,
        sl,
        tp,
        risk,
        reward,
        p.target_usd,
        spread,
        stop_spread_buffer=stop_spread_buffer,
        raw_chart_sl=raw_sl,
        stop_spread_policy=STOP_SPREAD_POLICY,
    )
