"""Frozen reaction -> bounded watch -> first affordable return. Shared with MT5."""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import ceil, floor, isfinite

from .candles import directional
from .config import Instrument, PriceAction, Rules
from .engine import Decision
from .orders import Plan, plan_entry, pnl

POLICIES = (
    "baseline",
    "return_cash",
    "return_wick",
    "pattern_wick",
    "pattern_zone",
    "pattern_managed",
    "return_managed",
    "sweep_wick",
    "sweep_managed",
    "sweep_pattern_managed",
    "reaction_managed",
)


def evidence(d: Decision) -> str:
    if not d.side:
        return "no_zone_signal"
    if directional(d.candle, -d.side):
        return "opposing_candle"
    if directional(d.candle, d.side):
        return "reaction_candle"
    if (
        d.prior_at_zone
        and directional(d.previous_candle, d.side)
        and d.side == d.bar_direction
        and d.side * d.momentum > 0
        and not ("doji" in d.candle or "inside" in d.candle)
    ):
        return "previous_pattern_follow_through"
    return "no_directional_pattern"


def stop_bid(d, s, pa):
    if not d.zone_id or min(d.trigger_low, d.trigger_high, d.invalidation) <= 0:
        raise ValueError("A frozen valid reaction and wick are required")
    anchor = (
        min(d.trigger_low, d.invalidation)
        if d.side == 1
        else max(d.trigger_high, d.invalidation)
    )
    unit = anchor / s.tick_size - d.side * pa.buffer_ticks
    return round(
        (floor(unit + 1e-9) if d.side == 1 else ceil(unit - 1e-9)) * s.tick_size, 12
    )


def execution_stop(bid_anchor, side, spread, tick):
    # On an MT5 bid chart a short is stopped by ASK, not by the bid high.
    return (
        bid_anchor
        if side == 1
        else round(ceil((bid_anchor + spread) / tick - 1e-9) * tick, 12)
    )


@dataclass(frozen=True, slots=True)
class Watch:
    d: Decision
    until_ms: int
    invalid_bid: float
    evidence: str
    policy: str

    def crossed(self, bid):
        if self.d.side * (bid - self.invalid_bid) <= 0:
            return "invalidated_before_entry"
        if self.d.objective is not None and self.d.side * (bid - self.d.objective) >= 0:
            return "objective_before_entry"
        return ""


def arm(d: Decision, s: Instrument, pa: PriceAction):
    if d.side not in (-1, 1) or not d.zone_id:
        return None, "no_zone_signal"
    ev = evidence(d)
    if pa.policy.startswith("sweep_") and d.reason != "sweep_reclaim":
        return None, "not_a_sweep"
    if ("pattern" in pa.policy) and ev not in (
        "reaction_candle",
        "previous_pattern_follow_through",
    ):
        return None, ev
    return Watch(
        d, (d.t + pa.ttl_seconds) * 1000, stop_bid(d, s, pa), ev, pa.policy
    ), "armed"


def cancel_at_close(w: Watch, context: Decision, now_ms: int):
    if now_ms >= w.until_ms:
        return "expired"
    if context.t > w.d.t and (
        context.side == -w.d.side
        or any(side == -w.d.side for _, side in context.breaks)
    ):
        return "opposite_structure_before_entry"
    return ""


def return_plan(
    w: Watch, s: Instrument, r: Rules, bid: float, ask: float, ms: int, min_stop=0.0
) -> tuple[Plan | None, str]:
    if ms < w.d.t * 1000 or ms >= w.until_ms:
        return None, "outside_watch_time"
    if not all(isfinite(v) for v in (bid, ask, min_stop)) or not 0 < bid <= ask:
        return None, "invalid_quote"
    d = w.d
    side = d.side
    if reason := w.crossed(bid):
        return None, reason
    pricing_d = d
    if w.policy.startswith("sweep_") or (
        w.policy == "reaction_managed" and d.reason == "sweep_reclaim"
    ):
        # A second test of the liquidity pocket itself, not a chase into the old range.
        far = d.level - side * d.width
        low, high = sorted((far, d.invalidation))
        if not low <= bid <= high:
            return None, "outside_swept_pocket"
        # Use this explicitly checked interval for the shared quote-location recheck.
        pricing_d = replace(
            d, level=(low + high) / 2, width=(high - low) / 2 + s.tick_size * 1e-7
        )
    elif abs(bid - d.level) > d.width + 1e-12:
        return None, "outside_return_band"
    if ask - bid > s.max_spread + 1e-10:
        return None, "spread_limit"
    sl = execution_stop(w.invalid_bid, side, ask - bid, s.tick_size)
    fill = (ask if side == 1 else bid) + side * s.slippage
    risk = -pnl(s, side, fill, sl - side * s.slippage)
    # Never clip the structural stop to make a wider-risk setup appear affordable.
    if not isfinite(risk) or risk <= 0 or risk > s.sl_usd + 1e-8:
        return None, "wick_risk_over_budget"
    mark = bid if side == 1 else ask
    if side * (mark - sl) < max(s.tick_size, min_stop) - 1e-10:
        return None, "wick_stop_too_close"
    plan, why = plan_entry(pricing_d, s, r, bid, ask, min_stop)
    if plan is None:
        return None, why
    # Rules supplied by callers are normalized once by rules_for().
    attached = plan.sl if w.policy == "return_cash" else sl
    return replace(
        plan,
        sl=attached,
        clears_structure=True,
        structural_stop=sl,
        structural_risk_usd=risk,
        max_structure_lots=s.lots * s.sl_usd / risk,
    ), "ready"


def rules_for(r, policy):
    return r.model_copy(
        update={
            "require_structural_stop": False,
            "exit_mode": "next_zone"
            if policy == "pattern_zone" or policy.endswith("_managed")
            else "fixed_cash",
        }
    )


def adverse(d: Decision, side: int, pa: PriceAction) -> str:
    if side * d.momentum > -pa.adverse_momentum:
        return ""
    if any(v == -side for _, v in d.breaks):
        return "adverse_BOS"
    if directional(d.candle, -side):
        return "adverse_candle"
    if side * d.prior_momentum <= -pa.adverse_momentum:
        return "two_adverse_closes"
    return ""


def protect(peak, mark, s, pa):
    peak = max(peak, mark)
    return peak, peak >= s.tp_usd and mark <= peak * (1 - pa.giveback_fraction)
