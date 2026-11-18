from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from math import isfinite, tanh
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from bot.features.liquidity import build_liquidity_features
from bot.features.structure import resample_m1, structure


TIMEFRAMES = {"M1": 1, "M5": 5, "M15": 15}
STRUCTURE_WEIGHTS = {"M1": 0.16, "M5": 0.20, "M15": 0.20}
TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15}


@dataclass(frozen=True)
class MarketDecision:
    bar_time: pd.Timestamp
    side: str | None
    score: float
    reason: str
    stop_loss: float | None = None
    take_profit: float | None = None
    stop_distance: float | None = None
    details: dict[str, float | int | str] | None = None


def session_allowed(timestamp: pd.Timestamp, sessions: dict) -> bool:
    """Apply configured kill-zone hours in their local timezone, including DST."""
    allowed = sessions.get("trade_only_in", [])
    if not allowed:
        return True
    zones = sessions.get("killzones", {})
    for name in allowed:
        zone = zones.get(name)
        if not zone:
            continue
        local = timestamp.tz_convert(ZoneInfo(str(zone.get("tz", "UTC"))))
        now = local.timetz().replace(tzinfo=None)
        start = time.fromisoformat(str(zone["start"]))
        end = time.fromisoformat(str(zone["end"]))
        is_inside = start <= now < end if start <= end else (now >= start or now < end)
        if is_inside:
            return True
    return False


def _causal_local_levels(
    bars: pd.DataFrame,
    swing_events: pd.DataFrame,
    current_state: pd.Series,
    timeframe: str,
    price: float,
    atr: float,
) -> dict[str, float]:
    """Find nearby levels and confirmed-pivot trendlines using closed bars only."""
    highs = bars["high"].astype(float)
    lows = bars["low"].astype(float)
    state_time = bars.index[-1]
    tf_minutes = TIMEFRAME_MINUTES[timeframe]
    levels = [
        float(highs.shift(1).tail(20).max()),
        float(lows.shift(1).tail(20).min()),
        float(current_state.get("latest_swing_high", np.nan)),
        float(current_state.get("latest_swing_low", np.nan)),
        float(current_state.get("protected_high", np.nan)),
        float(current_state.get("protected_low", np.nan)),
    ]
    confirmed = swing_events.loc[
        swing_events.get("event", pd.Series(dtype=str)).eq("swing_confirmed")
    ].copy() if not swing_events.empty else pd.DataFrame()
    trendline_support = float("nan")
    trendline_resistance = float("nan")

    if not confirmed.empty:
        for direction, target in (("low", "support"), ("high", "resistance")):
            pivots = confirmed.loc[confirmed["direction"].eq(direction)].tail(2)
            if len(pivots) < 2:
                continue
            first, second = pivots.iloc[0], pivots.iloc[1]
            first_pivot = pd.Timestamp(first["pivot_time"])
            second_pivot = pd.Timestamp(second["pivot_time"])
            confirmed_at = pd.Timestamp(second["time"])
            age = (state_time - confirmed_at).total_seconds() / (60 * tf_minutes)
            if age < 0 or age > 80:
                continue
            x0 = (first_pivot - second_pivot).total_seconds() / (60 * tf_minutes)
            if x0 == 0:
                continue
            slope = (float(first["level"]) - float(second["level"])) / x0
            # Only rising swing lows form a support line; only falling swing highs
            # form a resistance line. Both pivots were confirmed by completed closes.
            if target == "support" and slope > 0:
                trendline_support = float(second["level"])
            elif target == "resistance" and slope < 0:
                trendline_resistance = float(second["level"])
            else:
                continue
            elapsed = (state_time - second_pivot).total_seconds() / (60 * tf_minutes)
            projected = float(second["level"]) + slope * elapsed
            if target == "support":
                trendline_support = projected
            else:
                trendline_resistance = projected

    levels.extend((trendline_support, trendline_resistance))
    valid = [level for level in levels if isfinite(level)]
    below = [level for level in valid if level < price]
    above = [level for level in valid if level > price]
    nearest_support = max(below) if below else float("nan")
    nearest_resistance = min(above) if above else float("nan")
    safe_atr = max(float(atr), 1e-12)
    support_dist = max(0.0, (price - nearest_support) / safe_atr) if isfinite(nearest_support) else 99.0
    resistance_dist = max(0.0, (nearest_resistance - price) / safe_atr) if isfinite(nearest_resistance) else 99.0
    line_support_dist = abs(price - trendline_support) / safe_atr if isfinite(trendline_support) and price >= trendline_support else 99.0
    line_resistance_dist = abs(trendline_resistance - price) / safe_atr if isfinite(trendline_resistance) and price <= trendline_resistance else 99.0
    return {
        "support": nearest_support,
        "resistance": nearest_resistance,
        "support_distance_atr": support_dist,
        "resistance_distance_atr": resistance_dist,
        "trendline_support": trendline_support,
        "trendline_resistance": trendline_resistance,
        "trendline_support_distance_atr": line_support_dist,
        "trendline_resistance_distance_atr": line_resistance_dist,
    }


def evaluate_market(
    m1_bars: pd.DataFrame,
    timeframe: str,
    bid: float,
    ask: float,
    *,
    min_score: float = 0.66,
    target_r_multiple: float = 1.20,
    minimum_target_price_units: float = 1.50,
    minimum_net_reward_risk: float = 1.0,
    max_friction_to_stop_fraction: float = 0.20,
    round_trip_cost_ex_spread_price_units: float = 0.27,
    sessions: dict | None = None,
    require_protected_stop: bool = True,
    confluence_weights: dict | None = None,
    level_proximity_atr: float = 0.35,
    minimum_opposing_level_room_atr: float = 0.30,
    minimum_pullback_rejection: float = 0.12,
    pullback_proximity_by_timeframe: dict[str, float] | None = None,
    recent_stopouts: list[dict] | None = None,
) -> MarketDecision:
    """Score completed bars with current quote context and causal structure.

    The score is an uncalibrated confluence score, not a probability. Candle and
    swing features use completed bars; price-location features use the current
    bid/ask midpoint.
    """
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    if not (isfinite(bid) and isfinite(ask) and bid > 0 and ask >= bid):
        raise ValueError("A valid bid/ask quote is required")
    if not 0.5 <= min_score <= 1:
        raise ValueError("min_score must be between 0.5 and 1")
    if min(target_r_multiple, minimum_target_price_units, minimum_net_reward_risk) <= 0:
        raise ValueError("Target and reward/risk settings must be positive")
    if level_proximity_atr <= 0 or minimum_opposing_level_room_atr < 0:
        raise ValueError("Level distances must be non-negative and proximity must be positive")
    if not 0 <= minimum_pullback_rejection <= 1:
        raise ValueError("minimum_pullback_rejection must be between 0 and 1")

    bars_by_tf = {f"M{minutes}": resample_m1(m1_bars, minutes) for minutes in (1, 5, 15)}
    entry_bars = bars_by_tf[timeframe]
    if len(entry_bars) < 40:
        raise ValueError(f"Need at least 40 completed {timeframe} bars; got {len(entry_bars)}")

    structure_states: dict[str, pd.DataFrame] = {}
    structure_events: dict[str, pd.DataFrame] = {}
    for name, bars in bars_by_tf.items():
        if len(bars) < 20:
            return MarketDecision(entry_bars.index[-1], None, 0.5, f"insufficient_{name}_history")
        state, events = structure(bars, name, k=1.0)
        structure_states[name] = state
        structure_events[name] = events

    decision_time = entry_bars.index[-1]
    if sessions is not None and not session_allowed(decision_time, sessions):
        return MarketDecision(decision_time, None, 0.5, "outside_configured_session")

    weights = {f"structure_{name}": weight for name, weight in STRUCTURE_WEIGHTS.items()}
    supports: dict[str, float] = {}
    for name, state in structure_states.items():
        supports[f"structure_{name}"] = float(np.clip(state["trend"].iloc[-1], -1, 1))

    closes = entry_bars["close"].astype(float)
    highs = entry_bars["high"].astype(float)
    lows = entry_bars["low"].astype(float)
    opens = entry_bars["open"].astype(float)
    current_price = (float(bid) + float(ask)) / 2.0
    atr = float(structure_states[timeframe]["atr14"].iloc[-1])
    if not isfinite(atr) or atr <= 0:
        return MarketDecision(decision_time, None, 0.5, "invalid_atr")

    ema9 = closes.ewm(span=9, adjust=False, min_periods=9).mean().iloc[-1]
    ema21 = closes.ewm(span=21, adjust=False, min_periods=21).mean().iloc[-1]
    supports["ema_alignment"] = float(np.sign(ema9 - ema21))
    roc5_atr = float((closes.iloc[-1] - closes.iloc[-6]) / atr)
    supports["momentum_5bar"] = tanh(roc5_atr / 0.50)
    last_range = max(float(highs.iloc[-1] - lows.iloc[-1]), 1e-12)
    supports["candle_pressure"] = float(np.clip((closes.iloc[-1] - opens.iloc[-1]) / last_range, -1, 1))

    liquidity = build_liquidity_features(entry_bars)
    last_liquidity = liquidity.iloc[-1]
    supports["liquidity_sweep"] = float(last_liquidity["lower_sweep"] - last_liquidity["upper_sweep"])
    supports["fvg_direction"] = float(last_liquidity["fvg_bullish"] - last_liquidity["fvg_bearish"])
    volume = entry_bars.get("volume")
    volume_support = 0.0
    if volume is not None and len(volume) >= 20:
        rolling_volume = float(volume.iloc[-20:].iloc[:-1].mean())
        if rolling_volume > 0:
            surge = float(np.clip(float(volume.iloc[-1]) / rolling_volume - 1.0, 0, 1))
            volume_support = float(np.sign(supports["momentum_5bar"]) * surge)
    supports["directional_volume"] = volume_support

    bar_range = (highs - lows).replace(0, np.nan)
    candle_body = closes - opens
    body_fraction = float((abs(candle_body.iloc[-1]) / bar_range.iloc[-1]))
    upper_wick = float(highs.iloc[-1] - max(opens.iloc[-1], closes.iloc[-1]))
    lower_wick = float(min(opens.iloc[-1], closes.iloc[-1]) - lows.iloc[-1])
    upper_wick_ratio = float(np.clip(upper_wick / bar_range.iloc[-1], 0, 1))
    lower_wick_ratio = float(np.clip(lower_wick / bar_range.iloc[-1], 0, 1))
    candle_direction = float(np.sign(candle_body.iloc[-1]))
    aligned_wick_ratio = lower_wick_ratio if candle_direction > 0 else upper_wick_ratio
    supports["wickless_quality"] = candle_direction * body_fraction * (1 - aligned_wick_ratio)
    supports["wick_rejection"] = lower_wick_ratio - upper_wick_ratio
    signed_body_pressure = candle_body.div(bar_range).replace([np.inf, -np.inf], np.nan)
    supports["body_pressure_3"] = float(signed_body_pressure.tail(3).mean())
    supports["body_pressure_5"] = float(signed_body_pressure.tail(5).mean())
    supports["price_vs_ema21_atr"] = float(np.tanh((current_price - ema21) / atr))

    prior_high = highs.shift(1).tail(20).max()
    prior_low = lows.shift(1).tail(20).min()
    prior_range = float(prior_high - prior_low)
    raw_range_position = (
        float(np.clip(2 * (current_price - prior_low) / prior_range - 1, -1, 1))
        if isfinite(prior_range) and prior_range > 0 else 0.0
    )
    # Favor pullback locations: lower range supports buys, upper range supports sells.
    # This keeps a trend score from rewarding a short that is already pressed into lows.
    supports["range_position_20"] = -raw_range_position
    local_state = structure_states[timeframe].iloc[-1]
    local_levels = _causal_local_levels(
        entry_bars, structure_events[timeframe], local_state, timeframe, current_price, atr
    )
    m1_bars = bars_by_tf["M1"]
    m1_state = structure_states["M1"].iloc[-1]
    m1_levels = _causal_local_levels(
        m1_bars, structure_events["M1"], m1_state, "M1", current_price, float(m1_state["atr14"])
    )
    proximity = float((pullback_proximity_by_timeframe or {}).get(timeframe, level_proximity_atr))
    near_support = float(np.exp(-local_levels["support_distance_atr"] / proximity)) if local_levels["support_distance_atr"] < 99 else 0.0
    near_resistance = float(np.exp(-local_levels["resistance_distance_atr"] / proximity)) if local_levels["resistance_distance_atr"] < 99 else 0.0
    near_line_support = float(np.exp(-local_levels["trendline_support_distance_atr"] / proximity)) if local_levels["trendline_support_distance_atr"] < 99 else 0.0
    near_line_resistance = float(np.exp(-local_levels["trendline_resistance_distance_atr"] / proximity)) if local_levels["trendline_resistance_distance_atr"] < 99 else 0.0
    bullish_rejection = float(np.clip(
        0.5 * (lower_wick_ratio - upper_wick_ratio) + 0.5 * supports["candle_pressure"], -1, 1
    ))
    bearish_rejection = float(np.clip(
        0.5 * (upper_wick_ratio - lower_wick_ratio) - 0.5 * supports["candle_pressure"], -1, 1
    ))
    supports["m1_level_location"] = float(np.clip(
        np.exp(-m1_levels["support_distance_atr"] / level_proximity_atr)
        - np.exp(-m1_levels["resistance_distance_atr"] / level_proximity_atr), -1, 1
    )) if m1_levels["support_distance_atr"] < 99 and m1_levels["resistance_distance_atr"] < 99 else 0.0
    supports["m1_pullback_zone"] = float(np.clip(
        (np.exp(-m1_levels["support_distance_atr"] / level_proximity_atr) * bullish_rejection
         if m1_levels["support_distance_atr"] < 99 else 0.0)
        - (np.exp(-m1_levels["resistance_distance_atr"] / level_proximity_atr) * bearish_rejection
           if m1_levels["resistance_distance_atr"] < 99 else 0.0), -1, 1
    ))
    supports["local_level_location"] = float(np.clip(near_support - near_resistance, -1, 1))
    supports["local_pullback_zone"] = float(np.clip(
        (near_support + near_line_support) * bullish_rejection
        - (near_resistance + near_line_resistance) * bearish_rejection, -1, 1
    ))
    supports["trendline_proximity"] = float(np.clip(near_line_support - near_line_resistance, -1, 1))
    protected_low = float(structure_states[timeframe]["protected_low"].iloc[-1])
    protected_high = float(structure_states[timeframe]["protected_high"].iloc[-1])
    proximity_scale = max(atr * level_proximity_atr, 1e-12)
    level_reaction = 0.0
    available_levels = 0
    if isfinite(protected_low):
        near_low = float(np.exp(-abs(current_price - protected_low) / proximity_scale))
        level_reaction += near_low if current_price >= protected_low else -near_low
        available_levels += 1
    if isfinite(protected_high):
        near_high = float(np.exp(-abs(current_price - protected_high) / proximity_scale))
        level_reaction += -near_high if current_price <= protected_high else near_high
        available_levels += 1
    supports["protected_level_reaction"] = (
        float(np.clip(level_reaction / available_levels, -1, 1)) if available_levels else 0.0
    )
    prior_true_range = (highs - lows).shift(1).tail(20).median()
    range_expansion = (
        float(last_range / prior_true_range)
        if isfinite(float(prior_true_range)) and float(prior_true_range) > 0 else 1.0
    )
    feature_details = {
        "body_fraction": round(body_fraction, 4),
        "upper_wick_ratio": round(upper_wick_ratio, 4),
        "lower_wick_ratio": round(lower_wick_ratio, 4),
        "range_expansion_20": round(range_expansion, 4),
        "distance_to_buy_side_pool_atr": round(float(last_liquidity["draw_to_buy_side_atr"]), 4),
        "distance_to_sell_side_pool_atr": round(float(last_liquidity["draw_to_sell_side_atr"]), 4),
        "m1_nearest_support": round(m1_levels["support"], 5) if isfinite(m1_levels["support"]) else 0.0,
        "m1_nearest_resistance": round(m1_levels["resistance"], 5) if isfinite(m1_levels["resistance"]) else 0.0,
        "distance_to_m1_support_atr": round(m1_levels["support_distance_atr"], 4),
        "distance_to_m1_resistance_atr": round(m1_levels["resistance_distance_atr"], 4),
        "local_timeframe": timeframe,
        "local_nearest_support": round(local_levels["support"], 5) if isfinite(local_levels["support"]) else 0.0,
        "local_nearest_resistance": round(local_levels["resistance"], 5) if isfinite(local_levels["resistance"]) else 0.0,
        "distance_to_local_support_atr": round(local_levels["support_distance_atr"], 4),
        "distance_to_local_resistance_atr": round(local_levels["resistance_distance_atr"], 4),
        "local_trendline_support": round(local_levels["trendline_support"], 5) if isfinite(local_levels["trendline_support"]) else 0.0,
        "local_trendline_resistance": round(local_levels["trendline_resistance"], 5) if isfinite(local_levels["trendline_resistance"]) else 0.0,
        "distance_to_local_trendline_support_atr": round(local_levels["trendline_support_distance_atr"], 4),
        "distance_to_local_trendline_resistance_atr": round(local_levels["trendline_resistance_distance_atr"], 4),
        "local_near_support": round(near_support, 4),
        "local_near_resistance": round(near_resistance, 4),
        "m1_bullish_rejection": round(bullish_rejection, 4),
        "m1_bearish_rejection": round(bearish_rejection, 4),
    }

    weights = {
        "structure_M1": 0.10,
        "structure_M5": 0.10,
        "structure_M15": 0.10,
        "momentum_5bar": 0.10,
        "ema_alignment": 0.05,
        "candle_pressure": 0.03,
        "body_pressure_3": 0.06,
        "body_pressure_5": 0.06,
        "wickless_quality": 0.05,
        "wick_rejection": 0.04,
        "price_vs_ema21_atr": 0.06,
        "range_position_20": 0.04,
        "protected_level_reaction": 0.05,
        "m1_level_location": 0.03,
        "m1_pullback_zone": 0.05,
        "local_level_location": 0.12,
        "local_pullback_zone": 0.17,
        "trendline_proximity": 0.08,
        "liquidity_sweep": 0.04,
        "fvg_direction": 0.02,
        "directional_volume": 0.02,
    }
    if confluence_weights is not None:
        unknown_weights = set(confluence_weights).difference(weights)
        if unknown_weights:
            raise ValueError(f"Unknown confluence weight keys: {sorted(unknown_weights)}")
        for name, value in confluence_weights.items():
            if float(value) < 0:
                raise ValueError(f"Confluence weights cannot be negative: {name}")
            weights[name] = float(value)
    weight_total = sum(weights.values())
    if weight_total <= 0:
        raise ValueError("At least one confluence weight must be positive")
    directional_support = sum(weights[name] * supports[name] for name in weights) / weight_total
    consolidation = bool(structure_states[timeframe]["consolidation"].iloc[-1])
    long_score = float(np.clip(0.5 + 0.5 * directional_support - (0.03 if consolidation else 0), 0, 1))
    short_score = float(np.clip(0.5 - 0.5 * directional_support - (0.03 if consolidation else 0), 0, 1))
    side, score = ("BUY", long_score) if long_score >= short_score else ("SELL", short_score)
    details: dict[str, float | int | str] = {
        **{name: round(value, 4) for name, value in supports.items()},
        **feature_details,
        "long_score": round(long_score, 4),
        "short_score": round(short_score, 4),
        "atr": round(atr, 5),
        "consolidation": int(consolidation),
    }
    details["candidate_side"] = side
    near_opposing_level = (
        local_levels["support_distance_atr"] <= minimum_opposing_level_room_atr
        if side == "SELL" else local_levels["resistance_distance_atr"] <= minimum_opposing_level_room_atr
    )
    sell_pullback_confirmed = (
        (local_levels["resistance_distance_atr"] <= proximity
         or local_levels["trendline_resistance_distance_atr"] <= proximity)
        and bearish_rejection >= minimum_pullback_rejection
    )
    buy_pullback_confirmed = (
        (local_levels["support_distance_atr"] <= proximity
         or local_levels["trendline_support_distance_atr"] <= proximity)
        and bullish_rejection >= minimum_pullback_rejection
    )
    local_trend = int(local_state["trend"])
    ema_retest = abs(float(current_price - ema21)) <= max(0.45 * atr, 1e-12)
    sell_ema_pullback = local_trend <= 0 and current_price >= ema21 - 0.20 * atr and ema_retest and bearish_rejection >= minimum_pullback_rejection
    buy_ema_pullback = local_trend >= 0 and current_price <= ema21 + 0.20 * atr and ema_retest and bullish_rejection >= minimum_pullback_rejection
    sell_continuation_confirmed = (
        supports["price_vs_ema21_atr"] <= -0.15
        and supports["momentum_5bar"] <= -0.15
        and supports["candle_pressure"] <= 0.25
    )
    buy_continuation_confirmed = (
        supports["price_vs_ema21_atr"] >= 0.15
        and supports["momentum_5bar"] >= 0.15
        and supports["candle_pressure"] >= -0.25
    )
    if timeframe in {"M5", "M15"}:
        # Higher-timeframe continuation is allowed near the moving mean; this
        # avoids chasing stretched moves while retaining ordinary trend entries.
        sell_continuation_confirmed = sell_continuation_confirmed and local_trend <= 0 and abs(float(current_price - ema21)) <= 0.75 * atr
        buy_continuation_confirmed = buy_continuation_confirmed and local_trend >= 0 and abs(float(current_price - ema21)) <= 0.75 * atr
        sell_pullback_confirmed = sell_pullback_confirmed or sell_ema_pullback
        buy_pullback_confirmed = buy_pullback_confirmed or buy_ema_pullback
    if near_opposing_level:
        details["entry_location_setup"] = "too_close_to_local_opposing_level"
        reason = "sell_too_close_to_local_support" if side == "SELL" else "buy_too_close_to_local_resistance"
        return MarketDecision(decision_time, None, score, reason, details=details)
    if side == "SELL" and not (sell_pullback_confirmed or sell_continuation_confirmed):
        details["entry_location_setup"] = "short_local_pullback_or_controlled_continuation_unconfirmed"
        return MarketDecision(
            decision_time, None, score,
            "sell_needs_local_resistance_or_trendline_retest", details=details,
        )
    if side == "BUY" and not (buy_pullback_confirmed or buy_continuation_confirmed):
        details["entry_location_setup"] = "long_local_pullback_or_controlled_continuation_unconfirmed"
        return MarketDecision(
            decision_time, None, score,
            "buy_needs_local_support_or_trendline_retest", details=details,
        )
    details["entry_location_setup"] = (
        "resistance_or_trendline_rejection" if side == "SELL" and sell_pullback_confirmed else
        "support_or_trendline_rejection" if side == "BUY" and buy_pullback_confirmed else
        "bearish_continuation" if side == "SELL" else "bullish_continuation"
    )
    penalty, similar_losses = _recent_loss_penalty(side, details, recent_stopouts or [], timeframe)
    details["recent_similar_stopouts"] = similar_losses
    details["loss_feedback_penalty"] = round(penalty, 4)
    score = float(np.clip(score - penalty, 0, 1))
    if score < min_score:
        return MarketDecision(decision_time, None, score, "confluence_below_threshold_after_loss_feedback", details=details)

    state = structure_states[timeframe].iloc[-1]
    stop = float(state["protected_low"] if side == "BUY" else state["protected_high"])
    entry = ask if side == "BUY" else bid
    stop_distance = entry - stop if side == "BUY" else stop - entry
    if not isfinite(stop) or stop_distance <= 0:
        if require_protected_stop:
            return MarketDecision(decision_time, None, score, "no_valid_confirmed_protected_stop", details=details)
        stop_distance = atr
        stop = entry - stop_distance if side == "BUY" else entry + stop_distance
        details["stop_source"] = "atr_placeholder_cash_stop_will_replace"
    else:
        details["stop_source"] = "confirmed_protected_swing"

    target_distance = max(minimum_target_price_units, stop_distance * target_r_multiple)
    target = entry + target_distance if side == "BUY" else entry - target_distance
    spread = ask - bid
    estimated_cost = spread + round_trip_cost_ex_spread_price_units
    net_rr = (target_distance - estimated_cost) / (stop_distance + estimated_cost)
    friction_fraction = estimated_cost / stop_distance
    details.update({
        "stop_distance": round(stop_distance, 5),
        "target_distance": round(target_distance, 5),
        "estimated_net_reward_risk": round(net_rr, 4),
        "friction_to_stop_fraction": round(friction_fraction, 4),
        "spread_price_units": round(spread, 5),
    })
    if friction_fraction > max_friction_to_stop_fraction:
        return MarketDecision(decision_time, None, score, "friction_exceeds_stop_budget", details=details)
    if net_rr < minimum_net_reward_risk:
        return MarketDecision(decision_time, None, score, "reward_risk_after_costs", details=details)
    return MarketDecision(
        decision_time, side, score, "candidate", stop_loss=stop,
        take_profit=target, stop_distance=stop_distance, details=details,
    )


def _recent_loss_penalty(
    side: str,
    details: dict[str, float | int | str],
    stopouts: list[dict],
    timeframe: str,
) -> tuple[float, int]:
    """Apply a small, local penalty after repeated same-context stopouts."""
    setup = str(details.get("entry_location_setup", ""))
    trend = int(float(details.get(f"structure_{timeframe}", 0)))
    matching_events: set[str] = set()
    now_level = float(details.get("local_nearest_support" if side == "BUY" else "local_nearest_resistance", 0))
    atr = max(float(details.get("atr", 0)), 1e-12)
    for loss in stopouts[-20:]:
        if loss.get("timeframe") != timeframe or loss.get("side") != side or loss.get("setup") != setup:
            continue
        if int(loss.get("structure_trend", 99)) != trend:
            continue
        last_time = pd.to_datetime(loss.get("time_utc"), utc=True, errors="coerce")
        if pd.isna(last_time) or (pd.Timestamp.now(tz="UTC") - last_time).total_seconds() > 7 * 86400:
            continue
        old_level = float(loss.get("local_level", 0) or 0)
        if old_level > 0 and now_level > 0 and abs(old_level - now_level) <= max(0.35 * atr, 0.5):
            batch_id = str(loss.get("batch_id", "")).strip()
            event_key = batch_id or str(loss.get("time_utc", ""))[:19]
            if event_key:
                matching_events.add(event_key)
    matching = len(matching_events)
    return min(0.20, matching * 0.04), matching
