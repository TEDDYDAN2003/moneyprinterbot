from __future__ import annotations

import json
import hashlib
import math
import os
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from bot.decision.market_aware import TIMEFRAMES, evaluate_market
from bot.features.structure import resample_m1, structure


class DemoSafetyError(RuntimeError):
    """The requested demo run failed a fail-closed safety check."""


def run_demo(
    config: dict[str, Any],
    timeframe: str,
    *,
    armed: bool = False,
    poll_seconds: float = 2.0,
    max_iterations: int | None = None,
) -> None:
    """Observe MT5 and optionally send orders to the currently connected account."""
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {sorted(TIMEFRAMES)}")
    if poll_seconds < 0.5 or poll_seconds > 10:
        raise ValueError("poll_seconds must be between 0.5 and 10")
    run_config = config.get("run", {})
    run_mode = str(run_config.get("mode", "auto"))
    _validate_run_mode(run_mode, armed)
    mt5_config = config.get("mt5", {})
    mt5_config = dict(mt5_config)
    mt5_config["magic_number"] = int(mt5_config.get("magic_number", 20260101)) + {
        "M1": 1, "M5": 5, "M15": 15,
    }[timeframe]
    pilot = config.get("pilot", {})
    risk_config = config.get("risk", {})
    lot_config = config.get("lots", {})
    safety_config = config.get("safety", {})
    batch_targets = pilot.get("batch_target_profit_usd", {})
    money_target_active = timeframe in batch_targets
    target_r_multiple = _timeframe_setting(pilot, "target_r_multiple", timeframe, 1.50)
    cash_stop_enabled = _timeframe_setting(
        pilot, "per_entry_stop_loss_usd", timeframe, 0.0
    ) > 0
    min_net_rr = _timeframe_setting(pilot, "minimum_net_reward_risk", timeframe, 0.25)
    friction_limit = _timeframe_setting(pilot, "max_friction_to_stop_fraction", timeframe, 0.20)
    decision_min_net_rr = 0.01 if money_target_active else min_net_rr
    decision_min_target_units = (
        0.01 if money_target_active
        else _timeframe_setting(pilot, "minimum_target_price_units", timeframe, 1.50)
    )

    lock_path: Path | None = None
    owns_lock = False
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise DemoSafetyError('MT5 package missing; install with: pip install -e ".[live]"') from exc

    terminal_path = str(mt5_config.get("terminal_path", "")).strip()
    if not (mt5.initialize(terminal_path) if terminal_path else mt5.initialize()):
        raise ConnectionError(f"MT5 initialize failed: {mt5.last_error()}")

    try:
        terminal = mt5.terminal_info()
        account = mt5.account_info()
        if terminal is None or account is None or not bool(getattr(terminal, "connected", False)):
            raise DemoSafetyError("MT5 terminal is not connected to an account")
        account_type = _account_type(mt5, account)
        _validate_account_mode(account_type, armed)
        armed_login = int(account.login)
        armed_server = str(account.server)
        if armed and (
            not bool(getattr(terminal, "trade_allowed", False))
            or not bool(getattr(account, "trade_allowed", False))
            or not bool(getattr(account, "trade_expert", False))
        ):
            raise DemoSafetyError("MT5 terminal/account does not permit expert trading")
        if armed and str(getattr(account, "currency", "")).upper() != "USD":
            raise DemoSafetyError("Configured cash stop and target require a USD-denominated MT5 account")
        hedging_mode = getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", None)
        if armed and hedging_mode is not None and int(getattr(account, "margin_mode", -1)) != int(hedging_mode):
            raise DemoSafetyError(
                "Concurrent M1/M5/M15 workers and separate batch stops require an MT5 hedging account; "
                "a netting account merges XAUUSD positions and cannot keep each worker's stop independent"
            )

        symbol = str(run_config.get("symbol", "XAUUSD")) + str(run_config.get("broker_symbol_suffix", ""))
        if not mt5.symbol_select(symbol, True):
            raise DemoSafetyError(f"MT5 could not select broker symbol {symbol}")
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None or float(symbol_info.point) <= 0:
            raise DemoSafetyError(f"No usable symbol specification for {symbol}")
        if armed and int(symbol_info.trade_mode) == int(mt5.SYMBOL_TRADE_MODE_DISABLED):
            raise DemoSafetyError(f"Trading is disabled for {symbol}")

        account_key = f"{int(account.login)}_{''.join(c if c.isalnum() else '_' for c in str(account.server))}"
        configured_state_path = Path(str(pilot.get("state_file", "reports/demo_pilot_state.json")))
        state_path = configured_state_path.with_name(
            f"{configured_state_path.stem}_{account_key}{configured_state_path.suffix}"
        )
        account_log_name = f"_{account_key}"
        log_path = Path(
            str(pilot.get("log_file", "reports/demo_pilot_{timeframe}.jsonl"))
            .replace("{timeframe}", f"{timeframe}{account_log_name}")
        )
        deal_cursor_path = state_path.with_name(f"{state_path.stem}_{timeframe}.deals.json")
        deal_cursor = _load_deal_cursor(deal_cursor_path) if armed else None
        if armed:
            lock_path = state_path.with_name(f"{state_path.stem}_{timeframe}.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with lock_path.open("x", encoding="utf-8") as handle:
                    handle.write(json.dumps({"login": int(account.login), "server": str(account.server), "pid": os.getpid()}))
                owns_lock = True
            except FileExistsError as exc:
                if _remove_dead_lock(lock_path):
                    with lock_path.open("x", encoding="utf-8") as handle:
                        handle.write(json.dumps({"login": int(account.login), "server": str(account.server), "pid": os.getpid()}))
                    owns_lock = True
                else:
                    raise DemoSafetyError(f"Another armed worker may be running (lock file exists: {lock_path})") from exc

        _emit({
            "event": "account_worker_started",
            "account_login": int(account.login),
            "server": str(account.server),
            "account_type": account_type,
            "account_margin_mode": getattr(account, "margin_mode", None),
            "symbol": symbol,
            "timeframe": timeframe,
            "mode": "armed_orders" if armed else "shadow_only",
            "orders_enabled": bool(armed),
            "magic_number": mt5_config["magic_number"],
            "strategy_version": "cash_intrabar_confluence_v5_local_tf_pullbacks_profit_extension",
            "config_sha256": hashlib.sha256(
                json.dumps(config, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
            "strategy_score_warning": "confluence score is not a calibrated probability",
            "automatic_drawdown_halts": False,
        }, log_path)

        history_count = max(300, int(pilot.get("history_m1_bars", 2400)))
        initial_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 1, history_count)
        if initial_rates is None or len(initial_rates) < 300:
            raise DemoSafetyError(f"MT5 returned insufficient completed M1 history for {symbol}")
        initial_frame = _rates_frame(initial_rates)
        history_frame = initial_frame.copy()
        bars = history_frame
        last_seen_m1_open = initial_frame.index[-1]

        if armed:
            with _account_order_lock(state_path.with_suffix(".orders.lock")):
                pilot_state = _load_or_init_state(state_path, account, run_config)
                _save_state(state_path, pilot_state)
        else:
            pilot_state = None
        if armed:
            _emit({
                "event": "account_risk_baseline",
                "daily_start_equity": round(pilot_state["day_start_equity"], 2),
                "peak_equity": round(pilot_state["peak_equity"], 2),
                "fixed_lot_size": _timeframe_setting(pilot, "fixed_lot_size", timeframe, 0.05),
                "batch_size": int(pilot.get("batch_size", {}).get(timeframe, 1)),
                "note": "equity is logged for analysis; entries are not halted by drawdown",
            }, log_path)

        iteration = 0
        last_reported_halt: str | None = None
        last_market_log_state: str | None = None
        last_tick_signature: tuple | None = None
        last_decision_signature: tuple | None = None
        last_open_position_block: tuple | None = None
        close_attempted: set[int] = set()
        while max_iterations is None or iteration < max_iterations:
            iteration += 1
            tick = mt5.symbol_info_tick(symbol)
            account = mt5.account_info()
            if armed and account is not None:
                _ensure_same_demo_account(mt5, account, armed_login, armed_server)
                deal_cursor = _reconcile_exit_deals(
                    mt5, deal_cursor_path, deal_cursor, symbol, int(mt5_config["magic_number"]), log_path
                )
                deal_cursor = _track_open_positions(
                    mt5, deal_cursor_path, deal_cursor, symbol, int(mt5_config["magic_number"]), log_path
                )
            if tick is None or account is None:
                _emit({"event": "data_unavailable", "reason": "tick_or_account_missing"}, log_path)
                time.sleep(poll_seconds)
                continue
            tick_time = _tick_datetime(tick)
            kill_file = Path(str(safety_config.get("emergency_flatten_hotkey_file", "KILL")))
            if armed and kill_file.exists():
                with _account_order_lock(state_path.with_suffix(".orders.lock")):
                    _flatten_account_positions(mt5, mt5_config, log_path)
                time.sleep(poll_seconds)
                continue
            schedule_now = datetime.now(timezone.utc)
            can_trade_now, scheduled_flatten = _market_schedule(schedule_now, pilot, run_config)
            scheduled_positions = _positions(mt5, symbol=symbol) if armed else ()
            crossed_close = any(_opened_on_prior_broker_date(position, schedule_now, run_config) for position in scheduled_positions)
            if armed and (scheduled_flatten or crossed_close):
                with _account_order_lock(state_path.with_suffix(".orders.lock")):
                    close_attempted = _close_symbol_positions(
                        mt5, symbol, scheduled_positions, mt5_config, tick, close_attempted,
                        "crossed_market_close" if crossed_close else "scheduled_market_close", log_path,
                    )
            if not can_trade_now:
                market_state = "weekend_market_closed" if schedule_now.weekday() >= 5 else "daily_market_break"
                if last_market_log_state != market_state:
                    _emit({"event": "entry_blocked", "reason": market_state,
                           "checked_at_utc": schedule_now.isoformat()}, log_path)
                    last_market_log_state = market_state
                time.sleep(poll_seconds)
                continue
            if last_market_log_state != "market_open":
                _emit({"event": "market_window_open", "checked_at_utc": schedule_now.isoformat()}, log_path)
                last_market_log_state = "market_open"
            if (datetime.now(timezone.utc) - tick_time).total_seconds() > float(safety_config.get("stale_tick_seconds", 5)):
                _emit({"event": "stale_tick", "tick_time": tick_time.isoformat()}, log_path)
                time.sleep(poll_seconds)
                continue

            all_symbol_positions = _positions(mt5, symbol=symbol)
            pilot_positions = tuple(
                position for position in all_symbol_positions
                if int(getattr(position, "magic", -1)) == int(mt5_config["magic_number"])
            )
            open_positions = all_symbol_positions
            symbol_pending_orders = _orders(mt5, symbol=symbol)
            all_positions = _positions(mt5)
            kill_requested = kill_file.exists()
            if armed:
                with _account_order_lock(state_path.with_suffix(".orders.lock")):
                    pilot_state = _load_or_init_state(state_path, account, run_config)
                    pilot_state = _advance_state(pilot_state, float(account.equity), run_config)
                    _save_state(state_path, pilot_state)
            if armed and kill_requested:
                _flatten_account_positions(mt5, mt5_config, log_path)
                if last_reported_halt != "manual_kill_file":
                    _emit({"event": "manual_kill_switch", "new_orders": "blocked"}, log_path)
                    last_reported_halt = "manual_kill_file"
                time.sleep(poll_seconds)
                continue
            last_reported_halt = None

            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 1, 3)
            if rates is None or len(rates) == 0:
                time.sleep(poll_seconds)
                continue
            latest_bars = _rates_frame(rates)
            if latest_bars.index[-1] - last_seen_m1_open > pd.Timedelta(minutes=2):
                catchup = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 1, history_count)
                if catchup is None or len(catchup) < 300:
                    _emit({"event": "data_gap", "reason": "history_catchup_unavailable"}, log_path)
                    time.sleep(poll_seconds)
                    continue
                latest_bars = _rates_frame(catchup)
            new_bars = latest_bars.loc[latest_bars.index > last_seen_m1_open]
            has_new_bars = not new_bars.empty
            if has_new_bars:
                last_seen_m1_open = new_bars.index[-1]
                history_frame = pd.concat([history_frame, new_bars]).loc[
                    lambda value: ~value.index.duplicated(keep="last")
                ].tail(history_count)
            bars = history_frame
            if armed:
                close_attempted = _manage_higher_tf_positions(
                    mt5, symbol, pilot_positions, mt5_config, tick, close_attempted,
                    timeframe, pilot, bars, deal_cursor, deal_cursor_path, log_path,
                )
                close_attempted = _close_timed_out_positions(
                    mt5, symbol, pilot_positions, mt5_config, tick, close_attempted,
                    timeframe, pilot, bars, log_path,
                )
            tick_signature = (
                int(getattr(tick, "time_msc", 0)), float(tick.bid), float(tick.ask)
            )
            if not has_new_bars and tick_signature == last_tick_signature:
                time.sleep(poll_seconds)
                continue
            last_tick_signature = tick_signature

            try:
                decision = evaluate_market(
                    bars,
                    timeframe,
                    float(tick.bid),
                    float(tick.ask),
                    min_score=float(pilot.get("min_confluence_score", 0.56)),
                    target_r_multiple=target_r_multiple,
                    minimum_target_price_units=decision_min_target_units,
                    minimum_net_reward_risk=decision_min_net_rr,
                    max_friction_to_stop_fraction=(1000.0 if cash_stop_enabled else friction_limit),
                    round_trip_cost_ex_spread_price_units=float(
                        pilot.get("round_trip_cost_ex_spread_price_units", 0.27)
                    ),
                    sessions=config.get("sessions", {}),
                    require_protected_stop=not cash_stop_enabled,
                    confluence_weights=pilot.get("confluence_weights"),
                    level_proximity_atr=float(config.get("levels", {}).get("proximity_atr", 0.35)),
                    minimum_opposing_level_room_atr=float(config.get("levels", {}).get("minimum_opposing_level_room_atr", 0.30)),
                    minimum_pullback_rejection=float(config.get("levels", {}).get("minimum_pullback_rejection", 0.12)),
                    pullback_proximity_by_timeframe=config.get("levels", {}).get("pullback_proximity_by_timeframe", {}),
                    recent_stopouts=deal_cursor.get("recent_stopouts", []) if armed and deal_cursor else [],
                )
            except (ValueError, KeyError, IndexError) as exc:
                _emit({"event": "decision_error", "error": str(exc)}, log_path)
                time.sleep(poll_seconds)
                continue

            latest_tick = mt5.symbol_info_tick(symbol)
            if latest_tick is None or (
                datetime.now(timezone.utc) - _tick_datetime(latest_tick)
            ).total_seconds() > float(safety_config.get("stale_tick_seconds", 5)):
                _emit({"event": "quote_refresh_failed", "reason": "missing_or_stale_tick"}, log_path)
                time.sleep(poll_seconds)
                continue
            if float(latest_tick.bid) != float(tick.bid) or float(latest_tick.ask) != float(tick.ask):
                tick = latest_tick
                tick_time = _tick_datetime(tick)
                try:
                    decision = evaluate_market(
                        bars,
                        timeframe,
                        float(tick.bid),
                        float(tick.ask),
                        min_score=float(pilot.get("min_confluence_score", 0.56)),
                        target_r_multiple=target_r_multiple,
                        minimum_target_price_units=decision_min_target_units,
                        minimum_net_reward_risk=decision_min_net_rr,
                        max_friction_to_stop_fraction=(1000.0 if cash_stop_enabled else friction_limit),
                        round_trip_cost_ex_spread_price_units=float(
                            pilot.get("round_trip_cost_ex_spread_price_units", 0.27)
                        ),
                        sessions=config.get("sessions", {}),
                        require_protected_stop=not cash_stop_enabled,
                        confluence_weights=pilot.get("confluence_weights"),
                        level_proximity_atr=float(config.get("levels", {}).get("proximity_atr", 0.35)),
                        minimum_opposing_level_room_atr=float(config.get("levels", {}).get("minimum_opposing_level_room_atr", 0.30)),
                        minimum_pullback_rejection=float(config.get("levels", {}).get("minimum_pullback_rejection", 0.12)),
                        pullback_proximity_by_timeframe=config.get("levels", {}).get("pullback_proximity_by_timeframe", {}),
                        recent_stopouts=deal_cursor.get("recent_stopouts", []) if armed and deal_cursor else [],
                    )
                except (ValueError, KeyError, IndexError) as exc:
                    _emit({"event": "decision_error", "error": str(exc)}, log_path)
                    time.sleep(poll_seconds)
                    continue

            decision_details = dict(decision.details or {})
            logged_stop = decision.stop_loss
            logged_target = decision.take_profit
            if cash_stop_enabled:
                for key in (
                    "stop_source", "stop_distance", "target_distance",
                    "estimated_net_reward_risk", "friction_to_stop_fraction",
                ):
                    decision_details.pop(key, None)
                decision_details.update({
                    "execution_stop_mode": "cash_per_leg",
                    "cash_stop_usd_per_entry": _timeframe_setting(
                        pilot, "per_entry_stop_loss_usd", timeframe, 0.0
                    ),
                    "cash_target_usd_per_entry": (
                        float(batch_targets[timeframe])
                        / max(1, int(pilot.get("batch_size", {}).get(timeframe, 1)))
                        if timeframe in batch_targets else None
                    ),
                    "cash_stop_state": "calculated_after_signal_if_costs_allow",
                })
                logged_stop = None
                logged_target = None
            decision_signature = (
                decision.bar_time.isoformat(), decision.side, decision.reason, round(decision.score, 2)
            )
            if decision_signature != last_decision_signature:
                _emit({
                    "event": "market_decision",
                    "bar_time": decision.bar_time.isoformat(),
                    "quote_time": tick_time.isoformat(),
                    "quote_mid": round((float(tick.bid) + float(tick.ask)) / 2.0, 5),
                    "timeframe": timeframe,
                    "side": decision.side,
                    "score": round(decision.score, 4),
                    "reason": decision.reason,
                    "stop_loss": logged_stop,
                    "take_profit": logged_target,
                    "details": decision_details,
                }, log_path)
                last_decision_signature = decision_signature

            if decision.side is not None and not _rollover_allowed(
                tick_time, config.get("sessions", {}).get("rollover_blackout", {})
            ):
                _emit({"event": "entry_blocked", "reason": "rollover_blackout"}, log_path)
                time.sleep(poll_seconds)
                continue
            if armed and not bool(deal_cursor and deal_cursor.get("history_available", False)):
                _emit({"event": "entry_blocked", "reason": "trade_history_unavailable_for_realized_pnl_logging"}, log_path)
                time.sleep(poll_seconds)
                continue

            if not armed or decision.side is None:
                if not armed and decision.side is not None:
                    _emit({"event": "entry_not_sent", "reason": "shadow_only",
                           "bar_time": decision.bar_time.isoformat(), "timeframe": timeframe,
                           "side": decision.side, "score": round(decision.score, 4),
                           "stop_loss": decision.stop_loss, "take_profit": decision.take_profit}, log_path)
                time.sleep(poll_seconds)
                continue
            if kill_requested:
                _emit({"event": "kill_file_present", "path": str(kill_file), "new_orders": "blocked"}, log_path)
                time.sleep(poll_seconds)
                continue
            if open_positions or symbol_pending_orders:
                exposure_signature = (
                    tuple(sorted(int(position.ticket) for position in open_positions)),
                    tuple(sorted(int(order.ticket) for order in symbol_pending_orders)),
                )
                if exposure_signature != last_open_position_block:
                    _emit({"event": "entry_blocked", "reason": "symbol_exposure_already_open",
                           "open_tickets": exposure_signature[0],
                           "pending_order_tickets": exposure_signature[1],
                           "policy": "wait_until_all_symbol_exposure_closes"}, log_path)
                    last_open_position_block = exposure_signature
                time.sleep(poll_seconds)
                continue
            last_open_position_block = None
            spread_points = (float(tick.ask) - float(tick.bid)) / float(symbol_info.point)
            max_spread = float(min(risk_config.get("max_spread_points", 35), pilot.get("max_spread_points", 35)))
            if spread_points > max_spread:
                _emit({"event": "entry_blocked", "reason": "spread_limit", "spread_points": round(spread_points, 2)}, log_path)
                time.sleep(poll_seconds)
                continue
            if not _side_is_allowed(mt5, symbol_info, decision.side):
                _emit({"event": "entry_blocked", "reason": "symbol_side_disabled"}, log_path)
                time.sleep(poll_seconds)
                continue

            # Refresh quote, equity, positions, and guard state at the last
            # decision point before creating the market-order request.
            tick = mt5.symbol_info_tick(symbol)
            account = mt5.account_info()
            terminal = mt5.terminal_info()
            if account is not None:
                _ensure_same_demo_account(mt5, account, armed_login, armed_server)
            if tick is None or account is None or (
                datetime.now(timezone.utc) - _tick_datetime(tick)
            ).total_seconds() > float(safety_config.get("stale_tick_seconds", 5)):
                _emit({"event": "entry_blocked", "reason": "quote_or_account_stale"}, log_path)
                time.sleep(poll_seconds)
                continue
            if (
                terminal is None
                or not bool(getattr(terminal, "connected", False))
                or not bool(getattr(terminal, "trade_allowed", False))
                or not bool(getattr(account, "trade_allowed", False))
                or not bool(getattr(account, "trade_expert", False))
            ):
                _emit({"event": "entry_blocked", "reason": "terminal_trade_permission_unavailable"}, log_path)
                time.sleep(poll_seconds)
                continue
            if kill_file.exists():
                _emit({"event": "entry_blocked", "reason": "kill_file_present"}, log_path)
                time.sleep(poll_seconds)
                continue
            with _account_order_lock(state_path.with_suffix(".orders.lock")):
                pilot_state = _load_or_init_state(state_path, account, run_config)
                pilot_state = _advance_state(pilot_state, float(account.equity), run_config)
                _save_state(state_path, pilot_state)
            open_positions = _positions(mt5, symbol=symbol)
            symbol_pending_orders = _orders(mt5, symbol=symbol)
            all_positions = _positions(mt5)
            if open_positions or symbol_pending_orders:
                _emit({"event": "entry_blocked", "reason": "symbol_exposure_open_at_send"}, log_path)
                time.sleep(poll_seconds)
                continue
            tick_spread = (float(tick.ask) - float(tick.bid)) / float(symbol_info.point)
            if tick_spread > max_spread:
                _emit({"event": "entry_blocked", "reason": "spread_limit_at_send",
                       "spread_points": round(tick_spread, 2)}, log_path)
                time.sleep(poll_seconds)
                continue
            refreshed = evaluate_market(
                bars,
                timeframe,
                float(tick.bid),
                float(tick.ask),
                min_score=float(pilot.get("min_confluence_score", 0.56)),
                target_r_multiple=target_r_multiple,
                minimum_target_price_units=decision_min_target_units,
                minimum_net_reward_risk=decision_min_net_rr,
                max_friction_to_stop_fraction=(1000.0 if cash_stop_enabled else friction_limit),
                round_trip_cost_ex_spread_price_units=float(
                    pilot.get("round_trip_cost_ex_spread_price_units", 0.27)
                ),
                sessions=config.get("sessions", {}),
                require_protected_stop=not cash_stop_enabled,
                confluence_weights=pilot.get("confluence_weights"),
                level_proximity_atr=float(config.get("levels", {}).get("proximity_atr", 0.35)),
                minimum_opposing_level_room_atr=float(config.get("levels", {}).get("minimum_opposing_level_room_atr", 0.30)),
                minimum_pullback_rejection=float(config.get("levels", {}).get("minimum_pullback_rejection", 0.12)),
                pullback_proximity_by_timeframe=config.get("levels", {}).get("pullback_proximity_by_timeframe", {}),
                recent_stopouts=deal_cursor.get("recent_stopouts", []) if armed and deal_cursor else [],
            )
            if refreshed.side != decision.side:
                _emit({"event": "entry_blocked", "reason": "signal_changed_before_send",
                       "prior_side": decision.side, "refreshed_side": refreshed.side}, log_path)
                time.sleep(poll_seconds)
                continue
            decision = refreshed

            batch_size = max(1, int(pilot.get("batch_size", {}).get(timeframe, 1)))
            batch_id = (
                f"{timeframe}-{decision.bar_time.strftime('%Y%m%dT%H%M%S')}-"
                f"{int(getattr(tick, 'time_msc', 0))}-{decision.side}"
            )
            batch_target = pilot.get("batch_target_profit_usd", {}).get(timeframe)
            if batch_target is not None:
                if str(getattr(account, "currency", "")).upper() != "USD":
                    _emit({"event": "entry_blocked", "reason": "usd_target_requires_usd_account_currency",
                           "account_currency": getattr(account, "currency", None)}, log_path)
                    time.sleep(poll_seconds)
                    continue
            fixed_volume = _timeframe_setting(pilot, "fixed_lot_size", timeframe, 0.05)
            fallback_volume = _timeframe_setting(pilot, "fallback_lot_size", timeframe, 0.0)
            per_entry_stop_loss = _timeframe_setting(
                pilot, "per_entry_stop_loss_usd", timeframe, 0.0
            )
            if per_entry_stop_loss > 0 and str(getattr(account, "currency", "")).upper() != "USD":
                _emit({"event": "entry_blocked", "reason": "usd_stop_loss_requires_usd_account",
                       "account_currency": getattr(account, "currency", None)}, log_path)
                time.sleep(poll_seconds)
                continue

            def prepare_volume_order(volume: float):
                candidate = decision
                if per_entry_stop_loss > 0:
                    # Leave a small model margin for broker rounding and fill-price variance.
                    modeled_cash_limit = per_entry_stop_loss * 0.99
                    cash_stop = _money_risk_stop_price(
                        mt5, symbol, symbol_info, tick, decision.side, volume,
                        modeled_cash_limit, pilot, mt5_config,
                    )
                    if cash_stop is None:
                        return candidate, None, 0.0
                    candidate_details = dict(candidate.details or {})
                    candidate_details.update({
                        "stop_source": "cash_risk",
                        "stop_loss_usd_per_entry": per_entry_stop_loss,
                        "take_profit_usd_per_entry": (
                            float(batch_target) / batch_size if batch_target is not None else 0.0
                        ),
                    })
                    candidate = replace(
                        candidate, stop_loss=cash_stop, details=candidate_details
                    )
                if batch_target is not None:
                    target_price = _batch_target_price(
                        mt5, symbol, decision.side, tick, symbol_info, volume, batch_size,
                        float(batch_target), pilot, mt5_config,
                    )
                    if target_price is None:
                        return candidate, None, 0.0
                    candidate = replace(candidate, take_profit=target_price)
                order_pilot = dict(pilot)
                order_pilot["fixed_lot_size"] = volume
                order_pilot["cash_risk_stop"] = per_entry_stop_loss > 0
                candidate_request, candidate_risk = _build_order(
                    mt5, symbol, symbol_info, tick, account, candidate,
                    order_pilot, lot_config, mt5_config,
                )
                return candidate, candidate_request, candidate_risk

            def cash_stop_friction(candidate_request: dict) -> float:
                entry_price = float(tick.ask if decision.side == "BUY" else tick.bid)
                stop_distance = abs(entry_price - float(candidate_request["sl"]))
                estimated_cost = (
                    float(tick.ask) - float(tick.bid)
                    + float(pilot.get("round_trip_cost_ex_spread_price_units", 0.27))
                )
                return estimated_cost / max(stop_distance, 1e-12)

            volume_candidates = [fixed_volume]
            if 0 < fallback_volume < fixed_volume:
                volume_candidates.append(fallback_volume)
            prepared = None
            attempts: list[dict[str, Any]] = []
            for candidate_volume in volume_candidates:
                candidate_decision, candidate_request, candidate_risk = prepare_volume_order(candidate_volume)
                if candidate_request is None:
                    attempts.append({"volume": candidate_volume, "reason": "cash_stop_or_target_unavailable"})
                    continue
                friction_fraction = (
                    cash_stop_friction(candidate_request) if per_entry_stop_loss > 0
                    else float((candidate_decision.details or {}).get("friction_to_stop_fraction", 0.0))
                )
                if per_entry_stop_loss > 0 and candidate_risk > per_entry_stop_loss:
                    attempts.append({"volume": candidate_volume, "reason": "modeled_cash_risk_over_cap",
                                     "modeled_risk_usd": round(candidate_risk, 4)})
                    continue
                if friction_fraction > friction_limit and not cash_stop_enabled:
                    attempts.append({"volume": candidate_volume, "reason": "friction_over_limit",
                                     "friction_fraction": round(friction_fraction, 4)})
                    continue
                target_rr = (
                    float(batch_target) / max(candidate_risk * batch_size, 1e-12)
                    if batch_target is not None else float("inf")
                )
                if target_rr < min_net_rr:
                    attempts.append({"volume": candidate_volume, "reason": "net_reward_risk_below_limit",
                                     "net_reward_risk": round(target_rr, 4)})
                    continue
                prepared = (candidate_volume, candidate_decision, candidate_request, candidate_risk)
                break
            if prepared is None:
                _emit({"event": "entry_blocked",
                       "reason": "no_cash_stop_volume_meets_cost_risk_and_reward_limits"
                       if per_entry_stop_loss > 0 else "fixed_lot_or_target_unavailable",
                       "requested_volume": fixed_volume,
                       "fallback_volume": fallback_volume or None,
                       "cash_stop_usd_per_entry": per_entry_stop_loss or None,
                       "cash_target_usd_per_entry": (
                           float(batch_target) / batch_size if batch_target is not None else None
                       ),
                       "attempts": attempts}, log_path)
                time.sleep(poll_seconds)
                continue
            fixed_volume, decision, request, per_order_risk = prepared
            if cash_stop_enabled:
                decision_details = dict(decision.details or {})
                decision_details["cash_stop_friction_fraction"] = round(
                    cash_stop_friction(request), 4
                )
                decision_details["cash_stop_friction_policy"] = "logged_not_a_cash_mode_entry_gate"
                decision = replace(decision, details=decision_details)
            if fixed_volume != volume_candidates[0]:
                _emit({"event": "configured_volume_fallback_selected",
                       "requested_volume": volume_candidates[0], "selected_volume": fixed_volume,
                       "modeled_risk_per_entry_usd": round(per_order_risk, 4)}, log_path)
            calculated_risk = per_order_risk * batch_size
            requests = [dict(request) for _ in range(batch_size)]
            if batch_target is not None and float(batch_target) / max(calculated_risk, 1e-12) < min_net_rr:
                _emit({"event": "entry_blocked", "reason": "money_target_below_minimum_net_rr",
                       "target_profit_usd": float(batch_target), "batch_stop_risk": round(calculated_risk, 2),
                       "net_reward_risk": round(float(batch_target) / max(calculated_risk, 1e-12), 4)}, log_path)
                time.sleep(poll_seconds)
                continue
            if len(all_positions) + batch_size > int(risk_config.get("max_open_trades", 7)):
                _emit({"event": "entry_blocked", "reason": "max_open_trades"}, log_path)
                time.sleep(poll_seconds)
                continue
            if sum(float(position.volume) for position in all_positions) + float(request["volume"]) * batch_size > float(lot_config.get("max_total_lots_open", 0.35)) + 1e-9:
                _emit({"event": "entry_blocked", "reason": "max_total_lots_open"}, log_path)
                time.sleep(poll_seconds)
                continue
            stop_level = float(getattr(symbol_info, "trade_stops_level", 0)) * float(symbol_info.point)
            if decision.side == "BUY":
                stop_ok = float(tick.bid) - float(request["sl"]) >= stop_level
                target_ok = float(request["tp"]) - float(tick.ask) >= stop_level
            else:
                stop_ok = float(request["sl"]) - float(tick.ask) >= stop_level
                target_ok = float(tick.bid) - float(request["tp"]) >= stop_level
            if not stop_ok or not target_ok:
                _emit({"event": "entry_blocked", "reason": "broker_minimum_stop_distance"}, log_path)
                time.sleep(poll_seconds)
                continue

            precheck_failed = False
            rejected_check = None
            for request in requests:
                check = mt5.order_check(request)
                if check is None or int(getattr(check, "retcode", -1)) != 0:
                    rejected_check = check
                    precheck_failed = True
                    break
            if precheck_failed and 0 < fallback_volume < fixed_volume:
                fallback_decision, fallback_request, fallback_risk = prepare_volume_order(fallback_volume)
                fallback_target_ok = False
                if fallback_request is not None:
                    if decision.side == "BUY":
                        fallback_target_ok = float(fallback_request["tp"]) - float(tick.ask) >= stop_level
                    else:
                        fallback_target_ok = float(tick.bid) - float(fallback_request["tp"]) >= stop_level
                fallback_friction = (
                    cash_stop_friction(fallback_request)
                    if fallback_request is not None and per_entry_stop_loss > 0 else 0.0
                )
                fallback_batch_risk = fallback_risk * batch_size
                fallback_ok = (
                    fallback_request is not None
                    and fallback_target_ok
                    and (per_entry_stop_loss <= 0 or fallback_risk <= per_entry_stop_loss)
                    and (cash_stop_enabled or fallback_friction <= friction_limit)
                    and (batch_target is None or float(batch_target) / max(fallback_batch_risk, 1e-12) >= min_net_rr)
                )
                if fallback_ok:
                    fallback_requests = [dict(fallback_request) for _ in range(batch_size)]
                    fallback_rejection = None
                    for fallback_order in fallback_requests:
                        fallback_check = mt5.order_check(fallback_order)
                        if fallback_check is None or int(getattr(fallback_check, "retcode", -1)) != 0:
                            fallback_rejection = fallback_check
                            break
                    if fallback_rejection is None:
                        _emit({"event": "broker_volume_fallback_used",
                               "rejected_volume": fixed_volume,
                               "selected_volume": fallback_volume,
                               "original_retcode": getattr(rejected_check, "retcode", None),
                               "original_comment": getattr(rejected_check, "comment", "no response"),
                               "per_entry_risk_usd": round(fallback_risk, 4)}, log_path)
                        decision, request, per_order_risk = fallback_decision, fallback_request, fallback_risk
                        fixed_volume = fallback_volume
                        calculated_risk = fallback_batch_risk
                        requests = fallback_requests
                        precheck_failed = False
                    else:
                        _emit({"event": "broker_volume_fallback_rejected",
                               "selected_volume": fallback_volume,
                               "retcode": getattr(fallback_rejection, "retcode", None),
                               "comment": getattr(fallback_rejection, "comment", "no response")}, log_path)
                else:
                    _emit({"event": "broker_volume_fallback_blocked",
                           "selected_volume": fallback_volume,
                           "modeled_risk_per_entry_usd": round(fallback_risk, 4),
                           "per_entry_risk_limit_usd": per_entry_stop_loss,
                           "friction_fraction": round(fallback_friction, 4),
                           "max_friction_fraction": friction_limit,
                           "batch_risk_usd": round(fallback_batch_risk, 2),
                           "target_valid": fallback_target_ok}, log_path)
            if precheck_failed:
                _emit({"event": "order_precheck_rejected",
                       "retcode": getattr(rejected_check, "retcode", None),
                       "comment": getattr(rejected_check, "comment", "no response")}, log_path)
                time.sleep(poll_seconds)
                continue
            final_account = mt5.account_info()
            final_terminal = mt5.terminal_info()
            final_tick = mt5.symbol_info_tick(symbol)
            if final_account is None or final_terminal is None or final_tick is None:
                _emit({"event": "entry_blocked", "reason": "final_mt5_state_unavailable"}, log_path)
                time.sleep(poll_seconds)
                continue
            _ensure_same_demo_account(mt5, final_account, armed_login, armed_server)
            if (
                not bool(getattr(final_terminal, "connected", False))
                or not bool(getattr(final_terminal, "trade_allowed", False))
                or not bool(getattr(final_account, "trade_allowed", False))
                or not bool(getattr(final_account, "trade_expert", False))
            ):
                _emit({"event": "entry_blocked", "reason": "final_trade_permission_unavailable"}, log_path)
                time.sleep(poll_seconds)
                continue
            if (datetime.now(timezone.utc) - _tick_datetime(final_tick)).total_seconds() > float(safety_config.get("stale_tick_seconds", 5)):
                _emit({"event": "entry_blocked", "reason": "final_quote_stale"}, log_path)
                time.sleep(poll_seconds)
                continue
            deviation_price = int(mt5_config.get("deviation_points", 20)) * float(symbol_info.point)
            live_entry_price = float(final_tick.ask if decision.side == "BUY" else final_tick.bid)
            if abs(live_entry_price - float(request["price"])) > deviation_price:
                if not cash_stop_enabled:
                    _emit({"event": "entry_blocked", "reason": "price_moved_beyond_deviation"}, log_path)
                    time.sleep(poll_seconds)
                    continue
                try:
                    refreshed_cash_signal = evaluate_market(
                        bars, timeframe, float(final_tick.bid), float(final_tick.ask),
                        min_score=float(pilot.get("min_confluence_score", 0.56)),
                        target_r_multiple=target_r_multiple,
                        minimum_target_price_units=decision_min_target_units,
                        minimum_net_reward_risk=decision_min_net_rr,
                        max_friction_to_stop_fraction=1000.0,
                        round_trip_cost_ex_spread_price_units=float(
                            pilot.get("round_trip_cost_ex_spread_price_units", 0.27)
                        ),
                        sessions=config.get("sessions", {}),
                        require_protected_stop=False,
                        confluence_weights=pilot.get("confluence_weights"),
                        level_proximity_atr=float(config.get("levels", {}).get("proximity_atr", 0.35)),
                        minimum_opposing_level_room_atr=float(config.get("levels", {}).get("minimum_opposing_level_room_atr", 0.30)),
                        minimum_pullback_rejection=float(config.get("levels", {}).get("minimum_pullback_rejection", 0.12)),
                        pullback_proximity_by_timeframe=config.get("levels", {}).get("pullback_proximity_by_timeframe", {}),
                        recent_stopouts=deal_cursor.get("recent_stopouts", []) if armed and deal_cursor else [],
                    )
                except (ValueError, KeyError, IndexError) as exc:
                    _emit({"event": "entry_blocked", "reason": "cash_signal_refresh_failed",
                           "error": str(exc)}, log_path)
                    time.sleep(poll_seconds)
                    continue
                if refreshed_cash_signal.side != decision.side:
                    _emit({"event": "entry_blocked", "reason": "signal_changed_at_cash_reprice",
                           "prior_side": decision.side, "refreshed_side": refreshed_cash_signal.side}, log_path)
                    time.sleep(poll_seconds)
                    continue
                decision = refreshed_cash_signal
                tick = final_tick
                repriced = None
                repricing_volumes = [fixed_volume]
                if 0 < fallback_volume < fixed_volume:
                    repricing_volumes.append(fallback_volume)
                for repricing_volume in repricing_volumes:
                    repriced_decision, repriced_request, repriced_risk = prepare_volume_order(repricing_volume)
                    if repriced_request is None:
                        continue
                    if per_entry_stop_loss > 0 and repriced_risk > per_entry_stop_loss:
                        continue
                    repriced_batch_risk = repriced_risk * batch_size
                    if batch_target is not None and (
                        float(batch_target) / max(repriced_batch_risk, 1e-12) < min_net_rr
                    ):
                        continue
                    repriced = (repricing_volume, repriced_decision, repriced_request, repriced_risk)
                    break
                if repriced is None:
                    _emit({"event": "entry_blocked", "reason": "cash_stops_unavailable_at_refreshed_quote",
                           "requested_volume": fixed_volume,
                           "fallback_volume": fallback_volume or None}, log_path)
                    time.sleep(poll_seconds)
                    continue
                prior_price = float(request["price"])
                fixed_volume, decision, request, per_order_risk = repriced
                calculated_risk = per_order_risk * batch_size
                requests = [dict(request) for _ in range(batch_size)]
                final_precheck_failure = None
                for repriced_order in requests:
                    check = mt5.order_check(repriced_order)
                    if check is None or int(getattr(check, "retcode", -1)) != 0:
                        final_precheck_failure = check
                        break
                if final_precheck_failure is not None:
                    _emit({"event": "entry_blocked", "reason": "cash_repriced_order_precheck_rejected",
                           "retcode": getattr(final_precheck_failure, "retcode", None),
                           "comment": getattr(final_precheck_failure, "comment", "no response")}, log_path)
                    time.sleep(poll_seconds)
                    continue
                _emit({"event": "cash_order_repriced_at_send",
                       "prior_entry_price": prior_price,
                       "refreshed_entry_price": float(request["price"]),
                       "selected_volume": fixed_volume,
                       "modeled_risk_per_entry_usd": round(per_order_risk, 4),
                       "cash_target_profit_per_entry_usd": float(batch_target) / batch_size}, log_path)
            final_spread = (float(final_tick.ask) - float(final_tick.bid)) / float(symbol_info.point)
            if final_spread > max_spread:
                _emit({"event": "entry_blocked", "reason": "spread_limit_at_send",
                       "spread_points": round(final_spread, 2)}, log_path)
                time.sleep(poll_seconds)
                continue
            if kill_file.exists():
                _emit({"event": "entry_blocked", "reason": "kill_file_present_at_send"}, log_path)
                time.sleep(poll_seconds)
                continue
            pilot_state = _advance_state(pilot_state, float(final_account.equity), run_config)
            _save_state(state_path, pilot_state)
            final_all_positions = _positions(mt5)
            final_symbol_positions = tuple(
                position for position in final_all_positions
                if str(getattr(position, "symbol", "")) == symbol
            )
            final_symbol_orders = _orders(mt5, symbol=symbol)
            if final_symbol_positions or final_symbol_orders:
                _emit({"event": "entry_blocked", "reason": "symbol_position_open_at_send",
                       "open_tickets": [int(position.ticket) for position in final_symbol_positions],
                       "pending_order_tickets": [int(order.ticket) for order in final_symbol_orders]}, log_path)
                time.sleep(poll_seconds)
                continue
            if len(final_all_positions) + batch_size > int(risk_config.get("max_open_trades", 7)):
                _emit({"event": "entry_blocked", "reason": "max_open_trades_at_send"}, log_path)
                time.sleep(poll_seconds)
                continue
            if sum(float(position.volume) for position in final_all_positions) + float(request["volume"]) * batch_size > float(lot_config.get("max_total_lots_open", 0.35)) + 1e-9:
                _emit({"event": "entry_blocked", "reason": "max_total_lots_open_at_send"}, log_path)
                time.sleep(poll_seconds)
                continue
            with _account_order_lock(state_path.with_suffix(".orders.lock")):
                locked_account = mt5.account_info()
                if locked_account is None:
                    _emit({"event": "entry_blocked", "reason": "account_missing_under_order_lock"}, log_path)
                    time.sleep(poll_seconds)
                    continue
                _ensure_same_demo_account(mt5, locked_account, armed_login, armed_server)
                locked_state = _load_or_init_state(state_path, locked_account, run_config)
                locked_state = _advance_state(locked_state, float(locked_account.equity), run_config)
                _save_state(state_path, locked_state)
                locked_positions = _positions(mt5)
                locked_symbol_positions = tuple(
                    position for position in locked_positions
                    if str(getattr(position, "symbol", "")) == symbol
                )
                locked_symbol_orders = _orders(mt5, symbol=symbol)
                locked_tick = mt5.symbol_info_tick(symbol)
                locked_terminal = mt5.terminal_info()
                locked_price = (
                    float(locked_tick.ask if decision.side == "BUY" else locked_tick.bid)
                    if locked_tick is not None else math.inf
                )
                locked_spread = (
                    (float(locked_tick.ask) - float(locked_tick.bid)) / float(symbol_info.point)
                    if locked_tick is not None else math.inf
                )
                if (
                    kill_file.exists()
                    or not _market_schedule(datetime.now(timezone.utc), pilot, run_config)[0]
                    or locked_terminal is None
                    or not bool(getattr(locked_terminal, "connected", False))
                    or not bool(getattr(locked_terminal, "trade_allowed", False))
                    or not bool(getattr(locked_account, "trade_allowed", False))
                    or not bool(getattr(locked_account, "trade_expert", False))
                    or locked_symbol_positions
                    or locked_symbol_orders
                    or len(locked_positions) + batch_size > int(risk_config.get("max_open_trades", 7))
                    or sum(float(position.volume) for position in locked_positions) + float(request["volume"]) * batch_size
                    > float(lot_config.get("max_total_lots_open", 0.35)) + 1e-9
                    or locked_tick is None
                    or (datetime.now(timezone.utc) - _tick_datetime(locked_tick)).total_seconds()
                    > float(safety_config.get("stale_tick_seconds", 5))
                    or locked_spread > max_spread
                    or abs(locked_price - float(request["price"]))
                    > int(mt5_config.get("deviation_points", 20)) * float(symbol_info.point)
                ):
                    _emit({"event": "entry_blocked", "reason": "shared_account_guard_at_send"}, log_path)
                    time.sleep(poll_seconds)
                    continue
                accepted = {int(mt5.TRADE_RETCODE_DONE), int(mt5.TRADE_RETCODE_DONE_PARTIAL)}
                sent = 0
                sent_legs: list[dict[str, Any]] = []
                send_failed = False
                for request in requests:
                    result = mt5.order_send(request)
                    if result is None or int(getattr(result, "retcode", -1)) not in accepted:
                        _emit({"event": "order_send_rejected", "retcode": getattr(result, "retcode", None),
                               "comment": getattr(result, "comment", "no response"), "sent_in_batch": sent}, log_path)
                        send_failed = True
                        break
                    sent += 1
                    sent_legs.append({
                        "order_id": int(getattr(result, "order", 0)),
                        "deal_id": int(getattr(result, "deal", 0)),
                        "volume": float(request["volume"]),
                        "requested_entry_price": float(request["price"]),
                        "stop_loss": float(request["sl"]),
                        "take_profit": float(request["tp"]),
                    })
                if send_failed and sent:
                    _emit({
                        "event": "account_order_batch_partial",
                        "batch_id": batch_id,
                        "side": decision.side,
                        "timeframe": timeframe,
                        "decision_score": round(decision.score, 4),
                        "intended_legs": len(requests),
                        "sent_legs_count": sent,
                        "rollback_attempted": True,
                        "sent_legs": sent_legs,
                        "rejection_retcode": getattr(result, "retcode", None),
                        "rejection_comment": getattr(result, "comment", "no response"),
                    }, log_path)
                    opening_deals = deal_cursor.setdefault("opening_deals", {})
                    for leg in sent_legs:
                        if leg["deal_id"]:
                            opening_deals[str(leg["deal_id"])] = {
                                "timeframe": timeframe,
                                "batch_id": batch_id,
                                "batch_status": "partial_rollback_attempted",
                                "decision_reason": decision.reason,
                                "decision_score": round(decision.score, 4),
                                "signal_bar_time": decision.bar_time.isoformat(),
                                "decision_confluences": decision.details or {},
                                "stop_loss": leg["stop_loss"],
                                "take_profit": leg["take_profit"],
                                "requested_entry_price": leg["requested_entry_price"],
                                "open_order_id": leg["order_id"],
                            }
                    _save_deal_cursor(deal_cursor_path, deal_cursor)
                    deal_cursor = _track_open_positions(
                        mt5, deal_cursor_path, deal_cursor, symbol, int(mt5_config["magic_number"]), log_path,
                        entry_context={
                            "timeframe": timeframe,
                            "batch_id": batch_id,
                            "batch_status": "partial_rollback_attempted",
                            "decision_reason": decision.reason,
                            "decision_score": round(decision.score, 4),
                            "signal_bar_time": decision.bar_time.isoformat(),
                            "decision_confluences": decision.details or {},
                        },
                    )
                    fresh_tick = mt5.symbol_info_tick(symbol)
                    partial_positions = tuple(
                        position for position in _positions(mt5, symbol=symbol)
                        if int(getattr(position, "magic", -1)) == int(mt5_config["magic_number"])
                    )
                    if fresh_tick is not None:
                        _close_pilot_positions(mt5, symbol, partial_positions, mt5_config,
                                               fresh_tick, set(), "incomplete_batch_rollback", log_path)
                elif sent:
                    _emit({
                        "event": "account_order_batch_accepted",
                        "batch_id": batch_id,
                        "signal_bar_time": decision.bar_time.isoformat(),
                        "side": decision.side,
                        "timeframe": timeframe,
                        "decision_reason": decision.reason,
                        "decision_score": round(decision.score, 4),
                        "decision_confluences": decision.details or {},
                        "entry_count": sent,
                        "volume_per_entry": request["volume"],
                        "batch_volume": round(request["volume"] * sent, 8),
                        "batch_target_profit_usd": batch_target,
                        "stop_loss_usd_per_entry": per_entry_stop_loss or None,
                        "stop_loss": request["sl"],
                        "take_profit": request["tp"],
                        "sent_legs": sent_legs,
                        "estimated_batch_worst_case_risk_account_currency": round(calculated_risk, 2),
                    }, log_path)
                    opening_deals = deal_cursor.setdefault("opening_deals", {})
                    for leg in sent_legs:
                        if leg["deal_id"]:
                            opening_deals[str(leg["deal_id"])] = {
                                "timeframe": timeframe,
                                "batch_id": batch_id,
                                "decision_reason": decision.reason,
                                "decision_score": round(decision.score, 4),
                                "signal_bar_time": decision.bar_time.isoformat(),
                                "decision_confluences": decision.details or {},
                                "stop_loss": request["sl"],
                                "take_profit": request["tp"],
                                "requested_entry_price": leg["requested_entry_price"],
                                "open_order_id": leg["order_id"],
                            }
                    _save_deal_cursor(deal_cursor_path, deal_cursor)
                    deal_cursor = _track_open_positions(
                        mt5, deal_cursor_path, deal_cursor, symbol, int(mt5_config["magic_number"]), log_path,
                        entry_context={
                            "timeframe": timeframe,
                            "batch_id": batch_id,
                            "decision_reason": decision.reason,
                            "decision_score": round(decision.score, 4),
                            "signal_bar_time": decision.bar_time.isoformat(),
                            "decision_confluences": decision.details or {},
                        },
                    )
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        _emit({"event": "account_worker_stopped", "reason": "keyboard_interrupt"}, log_path if "log_path" in locals() else None)
    finally:
        try:
            mt5.shutdown()
        finally:
            if lock_path is not None and owns_lock:
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass


def _rates_frame(rates: Any) -> pd.DataFrame:
    frame = pd.DataFrame(rates)
    if frame.empty or "time" not in frame:
        raise DemoSafetyError("MT5 returned malformed M1 bars")
    frame.index = pd.to_datetime(frame.pop("time"), unit="s", utc=True)
    frame.index.name = "time"
    if "tick_volume" in frame:
        frame["volume"] = pd.to_numeric(frame["tick_volume"], errors="coerce").fillna(0)
    if "spread" not in frame:
        frame["spread"] = 0
    return frame[["open", "high", "low", "close", "volume", "spread"]].astype(float)


def _tick_datetime(tick: Any) -> datetime:
    milliseconds = getattr(tick, "time_msc", None)
    if milliseconds:
        return datetime.fromtimestamp(float(milliseconds) / 1000.0, tz=timezone.utc)
    return datetime.fromtimestamp(float(tick.time), tz=timezone.utc)


def _emit(event: dict[str, Any], path: Path | None = None) -> None:
    line = json.dumps(event, default=str, separators=(",", ":"))
    print(line, flush=True)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _load_deal_cursor(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                saved.setdefault("last_time_msc", 0)
                saved.setdefault("seen_tickets", [])
                saved.setdefault("position_totals", {})
                saved.setdefault("open_positions", {})
                saved.setdefault("opening_deals", {})
                saved.setdefault("recent_stopouts", [])
                saved.setdefault("history_available", False)
                return saved
        except (OSError, ValueError, TypeError):
            raise DemoSafetyError(f"Cannot read trade log cursor {path}; preserve it and inspect before restarting")
    return {
        "last_time_msc": int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000),
        "seen_tickets": [],
        "position_totals": {},
        "open_positions": {},
        "opening_deals": {},
        "recent_stopouts": [],
        "history_available": False,
    }


def _save_deal_cursor(path: Path, cursor: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(cursor, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _track_open_positions(
    mt5: Any,
    cursor_path: Path,
    cursor: dict[str, Any],
    symbol: str,
    magic: int,
    log_path: Path,
    entry_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    changed = False
    contexts = cursor.setdefault("open_positions", {})
    for position in _positions(mt5, symbol=symbol):
        if int(getattr(position, "magic", -1)) != magic:
            continue
        position_id = int(getattr(position, "identifier", getattr(position, "ticket", 0)))
        key = str(position_id)
        is_buy = int(position.type) == int(mt5.POSITION_TYPE_BUY)
        live_values = {
            "entry_price": float(position.price_open),
            "volume": float(position.volume),
            "stop_loss": float(getattr(position, "sl", 0.0)),
            "take_profit": float(getattr(position, "tp", 0.0)),
        }
        if key in contexts:
            current_context = contexts[key]
            initial_sl = float(current_context.get("initial_stop_loss", live_values["stop_loss"]))
            current_context.setdefault("initial_stop_loss", initial_sl)
            current_context.setdefault(
                "initial_risk_distance",
                abs(float(current_context.get("entry_price", live_values["entry_price"])) - initial_sl)
                if initial_sl > 0 else 0.0,
            )
            if any(contexts[key].get(name) != value for name, value in live_values.items()):
                contexts[key].update(live_values)
                _emit({"event": "position_protection_snapshot", "position_id": position_id,
                       "symbol": symbol, "timeframe_magic": magic, **live_values}, log_path)
                changed = True
            continue
        context = {
            "position_id": position_id,
            "symbol": symbol,
            "timeframe_magic": magic,
            "side": "BUY" if is_buy else "SELL",
            "entry_time_utc": datetime.fromtimestamp(float(position.time), timezone.utc).isoformat(),
            **live_values,
            "comment": str(getattr(position, "comment", "")),
        }
        context["initial_stop_loss"] = live_values["stop_loss"]
        context["initial_risk_distance"] = (
            abs(live_values["entry_price"] - live_values["stop_loss"])
            if live_values["stop_loss"] > 0 else 0.0
        )
        if entry_context:
            context.update(entry_context)
        contexts[key] = context
        _emit({"event": "position_opened", "source": "live_position_snapshot", **context}, log_path)
        changed = True
    if changed:
        _save_deal_cursor(cursor_path, cursor)
    return cursor


def _reconcile_exit_deals(
    mt5: Any,
    cursor_path: Path,
    cursor: dict[str, Any],
    symbol: str,
    magic: int,
    log_path: Path,
) -> dict[str, Any]:
    last_time_msc = int(cursor.get("last_time_msc", 0))
    start = datetime.fromtimestamp(max(0, last_time_msc - 2000) / 1000, timezone.utc)
    end = datetime.now(timezone.utc) + timedelta(seconds=1)
    deals = mt5.history_deals_get(start, end)
    if deals is None:
        cursor["history_available"] = False
        error = mt5.last_error()
        error_text = str(error)
        if cursor.get("history_error") != error_text:
            _emit({"event": "trade_history_unavailable", "error": error_text}, log_path)
            cursor["history_error"] = error_text
            _save_deal_cursor(cursor_path, cursor)
        return cursor
    had_history_error = cursor.pop("history_error", None) is not None
    cursor["history_available"] = True
    seen = set(int(ticket) for ticket in cursor.get("seen_tickets", []))
    new_seen: list[int] = []
    totals: dict[str, dict[str, float]] = cursor.setdefault("position_totals", {})
    contexts: dict[str, dict[str, Any]] = cursor.setdefault("open_positions", {})
    opening_deals: dict[str, dict[str, Any]] = cursor.setdefault("opening_deals", {})
    active_ids = {
        str(int(getattr(position, "identifier", getattr(position, "ticket", 0))))
        for position in _positions(mt5, symbol=symbol)
        if int(getattr(position, "magic", -1)) == magic
    }
    maximum_time = last_time_msc
    for deal in sorted(deals, key=lambda item: (int(getattr(item, "time_msc", 0)), int(getattr(item, "ticket", 0)))):
        ticket = int(getattr(deal, "ticket", 0))
        time_msc = int(getattr(deal, "time_msc", 0))
        maximum_time = max(maximum_time, time_msc)
        if ticket in seen:
            continue
        seen.add(ticket)
        new_seen.append(ticket)
        if str(getattr(deal, "symbol", "")) != symbol or int(getattr(deal, "magic", -1)) != magic:
            continue
        position_id = int(getattr(deal, "position_id", 0))
        position_key = str(position_id)
        profit = float(getattr(deal, "profit", 0.0))
        commission = float(getattr(deal, "commission", 0.0))
        swap = float(getattr(deal, "swap", 0.0))
        fee = float(getattr(deal, "fee", 0.0))
        deal_net = profit + commission + swap + fee
        position_total = totals.setdefault(position_key, {
            "profit": 0.0, "commission": 0.0, "swap": 0.0, "fee": 0.0,
        })
        position_total["profit"] += profit
        position_total["commission"] += commission
        position_total["swap"] += swap
        position_total["fee"] += fee
        entry = int(getattr(deal, "entry", -1))
        if entry == int(getattr(mt5, "DEAL_ENTRY_IN", 0)):
            opening_context = opening_deals.pop(str(ticket), {})
            if position_key in contexts:
                contexts[position_key].update(opening_context)
            else:
                deal_type = int(getattr(deal, "type", -1))
                is_buy = deal_type == int(getattr(mt5, "DEAL_TYPE_BUY", 0))
                contexts[position_key] = {
                    "position_id": position_id,
                    "symbol": symbol,
                    "timeframe_magic": magic,
                    "side": "BUY" if is_buy else "SELL",
                    "entry_time_utc": datetime.fromtimestamp(time_msc / 1000, timezone.utc).isoformat(),
                    "entry_price": float(getattr(deal, "price", 0.0)),
                    "volume": float(getattr(deal, "volume", 0.0)),
                    **opening_context,
                }
                _emit({"event": "position_opened", "source": "opening_deal_history",
                       **contexts[position_key]}, log_path)
        exit_entries = {
            int(getattr(mt5, "DEAL_ENTRY_OUT", 1)),
            int(getattr(mt5, "DEAL_ENTRY_OUT_BY", 3)),
            int(getattr(mt5, "DEAL_ENTRY_INOUT", 2)),
        }
        if entry not in exit_entries:
            continue
        reason_code = int(getattr(deal, "reason", -1))
        deal_comment = str(getattr(deal, "comment", ""))
        if reason_code == int(getattr(mt5, "DEAL_REASON_TP", -1001)):
            close_reason = "take_profit"
        elif reason_code == int(getattr(mt5, "DEAL_REASON_SL", -1002)):
            close_reason = "stop_loss"
        elif reason_code == int(getattr(mt5, "DEAL_REASON_SO", -1003)):
            close_reason = "stop_out"
        elif "scheduled_market_close" in deal_comment or "crossed_market_close" in deal_comment:
            close_reason = "scheduled_market_close"
        elif "time_limit_exit" in deal_comment:
            close_reason = "time_limit_exit"
        elif "incomplete_batch_rollback" in deal_comment:
            close_reason = "incomplete_batch_rollback"
        elif "risk_halt" in deal_comment or "manual_kill_flatten" in deal_comment:
            close_reason = "risk_or_manual_flatten"
        else:
            close_reason = "other_or_manual_close"
        context = contexts.get(position_key, {})
        is_closed = position_key not in active_ids
        cumulative_net = sum(float(value) for value in position_total.values())
        _emit({
            "event": "position_exit_deal",
            "deal_ticket": ticket,
            "position_id": position_id,
            "order_ticket": int(getattr(deal, "order", 0)),
            "time_utc": datetime.fromtimestamp(time_msc / 1000, timezone.utc).isoformat(),
            "symbol": symbol,
            "timeframe_magic": magic,
            "close_reason": close_reason,
            "position_closed": is_closed,
            "side": context.get("side"),
            "volume": float(getattr(deal, "volume", 0.0)),
            "exit_price": float(getattr(deal, "price", 0.0)),
            "deal_comment": deal_comment,
            "stop_loss": context.get("stop_loss"),
            "take_profit": context.get("take_profit"),
            "deal_profit": round(profit, 2),
            "deal_commission": round(commission, 2),
            "deal_swap": round(swap, 2),
            "deal_fee": round(fee, 2),
            "deal_net_pnl": round(deal_net, 2),
            "position_cumulative_net_pnl": round(cumulative_net, 2),
            "outcome": ("win" if cumulative_net > 0 else "loss" if cumulative_net < 0 else "breakeven") if is_closed else "partial_exit",
            "entry_context": context,
        }, log_path)
        if is_closed:
            if cumulative_net < 0 and close_reason == "stop_loss":
                confluences = context.get("decision_confluences", {})
                side = context.get("side")
                local_level_key = "local_nearest_support" if side == "BUY" else "local_nearest_resistance"
                cursor.setdefault("recent_stopouts", []).append({
                    "time_utc": datetime.fromtimestamp(time_msc / 1000, timezone.utc).isoformat(),
                    "timeframe": str(context.get("timeframe", "")),
                    "side": side,
                    "batch_id": str(context.get("batch_id", "")),
                    "setup": str(confluences.get("entry_location_setup", "")),
                    "structure_trend": int(float(confluences.get(f"structure_{context.get('timeframe', '')}", 0))),
                    "local_level": float(confluences.get(local_level_key, 0) or 0),
                })
                cursor["recent_stopouts"] = cursor["recent_stopouts"][-20:]
            totals.pop(position_key, None)
            contexts.pop(position_key, None)
    if new_seen or maximum_time != last_time_msc or had_history_error:
        cursor["last_time_msc"] = maximum_time
        cursor["seen_tickets"] = (cursor.get("seen_tickets", []) + new_seen)[-2000:]
        _save_deal_cursor(cursor_path, cursor)
    return cursor


def _positions(mt5: Any, **filters: Any) -> tuple[Any, ...]:
    result = mt5.positions_get(**filters)
    if result is not None:
        return tuple(result)
    error = mt5.last_error()
    if error and int(error[0]) != 1:
        raise DemoSafetyError(f"Cannot verify open positions: {error}")
    return ()


def _orders(mt5: Any, **filters: Any) -> tuple[Any, ...]:
    result = mt5.orders_get(**filters)
    if result is not None:
        return tuple(result)
    error = mt5.last_error()
    if error and int(error[0]) != 1:
        raise DemoSafetyError(f"Cannot verify pending orders: {error}")
    return ()


def _close_symbol_positions(
    mt5: Any,
    symbol: str,
    positions: tuple[Any, ...],
    mt5_config: dict,
    tick: Any,
    attempted: set[int],
    reason: str,
    log_path: Path | None = None,
) -> set[int]:
    """Close every position for this bot's symbol, regardless of timeframe magic."""
    for position in positions:
        position_config = dict(mt5_config)
        position_config["magic_number"] = int(getattr(position, "magic", mt5_config["magic_number"]))
        attempted = _close_pilot_positions(
            mt5, symbol, (position,), position_config, tick, attempted, reason, log_path
        )
    return attempted


def _account_type(mt5: Any, account: Any) -> str:
    if account is None:
        raise DemoSafetyError("MT5 did not return account information")
    mode = int(getattr(account, "trade_mode", -1))
    if mode == int(mt5.ACCOUNT_TRADE_MODE_DEMO):
        return "demo"
    if mode == int(getattr(mt5, "ACCOUNT_TRADE_MODE_CONTEST", 1)):
        return "contest"
    if mode == int(getattr(mt5, "ACCOUNT_TRADE_MODE_REAL", 2)):
        return "real"
    raise DemoSafetyError(f"MT5 returned an unrecognized account trade mode: {mode}")


def _validate_run_mode(mode: str, armed: bool) -> None:
    if mode not in {"auto", "demo", "live", "paper"}:
        raise DemoSafetyError(f"Unsupported run.mode={mode!r}")
    if armed:
        raise DemoSafetyError("Legacy runner is shadow-only; use the zone scanner in explicit demo mode")


def _validate_account_mode(account_type: str, armed: bool) -> None:
    if armed and account_type != "demo":
        raise DemoSafetyError("Refusing orders on a non-demo account")


def _ensure_same_demo_account(mt5: Any, account: Any, login: int, server: str) -> None:
    _validate_account_mode(_account_type(mt5, account), armed=True)
    if int(account.login) != login or str(account.server) != server:
        raise DemoSafetyError("MT5 account changed during the armed run; stopped before sending more orders")


def _market_schedule(tick_time: datetime, pilot: dict, run_config: dict) -> tuple[bool, bool]:
    """Return (entries_allowed, flatten_now) in broker-local time.

    The configured flatten/reopen times must match the broker's published sessions.
    """
    timezone_name = str(run_config.get("market_close_timezone", run_config.get("timezone_broker", "UTC")))
    try:
        local = tick_time.astimezone(ZoneInfo(timezone_name))
    except Exception:
        local = tick_time.astimezone(timezone.utc)
    current = local.timetz().replace(tzinfo=None)
    if local.weekday() >= 5:
        return False, True
    close_at = datetime.strptime(str(pilot.get("daily_flatten_time", "23:45")), "%H:%M").time()
    reopen_at = datetime.strptime(str(pilot.get("daily_reopen_time", "01:05")), "%H:%M").time()
    if reopen_at < close_at:
        in_closed_window = current >= close_at or current < reopen_at
        flatten_now = current >= close_at
    else:
        in_closed_window = reopen_at <= current < close_at
        flatten_now = False
    if local.weekday() == 4 and current >= close_at:
        return False, True
    if in_closed_window:
        return False, flatten_now
    return True, False


def _rollover_allowed(timestamp: Any, blackout: dict) -> bool:
    if not blackout:
        return True
    ts = pd.Timestamp(timestamp)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    local = ts.tz_convert(ZoneInfo(str(blackout.get("tz", "UTC"))))
    current = local.timetz().replace(tzinfo=None)
    start = datetime.strptime(str(blackout["start"]), "%H:%M").time()
    end = datetime.strptime(str(blackout["end"]), "%H:%M").time()
    inside = start <= current < end if start <= end else (current >= start or current < end)
    return not inside


def _opened_on_prior_broker_date(position: Any, now: datetime, run_config: dict) -> bool:
    try:
        zone = _account_timezone(run_config)
        opened = datetime.fromtimestamp(float(position.time), tz=timezone.utc).astimezone(zone)
        current = now.astimezone(zone)
        return opened.date() < current.date()
    except Exception:
        return True


def _account_timezone(run_config: dict) -> ZoneInfo:
    try:
        return ZoneInfo(str(run_config.get("timezone_broker", "UTC")))
    except Exception:
        return ZoneInfo("UTC")


def _timeframe_setting(config: dict, key: str, timeframe: str, default: float) -> float:
    value = config.get(key, default)
    if isinstance(value, dict):
        value = value.get(timeframe, default)
    return float(value)


def _load_or_init_state(path: Path, account: Any, run_config: dict) -> dict[str, Any]:
    local_now = datetime.now(_account_timezone(run_config))
    today = local_now.date().isoformat()
    week = f"{local_now.isocalendar().year}-W{local_now.isocalendar().week:02d}"
    login, server = int(account.login), str(account.server)
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise DemoSafetyError(f"Cannot read demo risk state {path}: {exc}") from exc
        if int(saved.get("login", -1)) != login or str(saved.get("server", "")) != server:
            raise DemoSafetyError("Demo risk state belongs to another account; resolve it before arming")
        if saved.get("date") != today:
            saved["date"] = today
            saved["day_start_equity"] = float(account.equity)
        if saved.get("week") != week:
            saved["week"] = week
            saved["week_start_equity"] = float(account.equity)
        saved.setdefault("week_start_equity", float(account.equity))
        saved["peak_equity"] = max(float(saved["peak_equity"]), float(account.equity))
        return saved
    return {
        "date": today,
        "login": login,
        "server": server,
        "day_start_equity": float(account.equity),
        "week": week,
        "week_start_equity": float(account.equity),
        "peak_equity": float(account.equity),
    }


def _advance_state(state: dict[str, Any], equity: float, run_config: dict) -> dict[str, Any]:
    local_now = datetime.now(_account_timezone(run_config))
    today = local_now.date().isoformat()
    week = f"{local_now.isocalendar().year}-W{local_now.isocalendar().week:02d}"
    if state.get("date") != today:
        state["date"] = today
        state["day_start_equity"] = equity
    if state.get("week") != week:
        state["week"] = week
        state["week_start_equity"] = equity
    state.setdefault("week_start_equity", equity)
    state["peak_equity"] = max(float(state["peak_equity"]), equity)
    return state


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _side_is_allowed(mt5: Any, symbol_info: Any, side: str) -> bool:
    mode = int(symbol_info.trade_mode)
    if mode == int(mt5.SYMBOL_TRADE_MODE_DISABLED) or mode == int(mt5.SYMBOL_TRADE_MODE_CLOSEONLY):
        return False
    if side == "BUY" and mode == int(mt5.SYMBOL_TRADE_MODE_SHORTONLY):
        return False
    if side == "SELL" and mode == int(mt5.SYMBOL_TRADE_MODE_LONGONLY):
        return False
    return True


def _build_order(
    mt5: Any,
    symbol: str,
    symbol_info: Any,
    tick: Any,
    account: Any,
    decision: Any,
    pilot: dict,
    lots: dict,
    mt5_config: dict,
) -> tuple[dict[str, Any] | None, float]:
    side = decision.side
    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    entry = float(tick.ask if side == "BUY" else tick.bid)
    digits = int(symbol_info.digits)
    scale = 10 ** digits
    stop = float(decision.stop_loss)
    target = float(decision.take_profit)
    if side == "BUY":
        if not bool(pilot.get("cash_risk_stop", False)):
            stop = math.floor(stop * scale) / scale
        target = math.ceil(target * scale) / scale
    else:
        if not bool(pilot.get("cash_risk_stop", False)):
            stop = math.ceil(stop * scale) / scale
        target = math.floor(target * scale) / scale
    if stop <= 0 or target <= 0 or (side == "BUY" and not stop < entry < target) or (side == "SELL" and not target < entry < stop):
        return None, 0.0
    slippage = max(
        float(pilot.get("assumed_slippage_price_units_per_side", 0.10)),
        int(mt5_config.get("deviation_points", 20)) * float(symbol_info.point),
    )
    worst_entry = entry + slippage if side == "BUY" else entry - slippage
    worst_stop = stop - slippage if side == "BUY" else stop + slippage
    one_lot_pnl = mt5.order_calc_profit(order_type, symbol, 1.0, worst_entry, worst_stop)
    if one_lot_pnl is None or not math.isfinite(float(one_lot_pnl)) or float(one_lot_pnl) >= 0:
        return None, 0.0
    commission = float(pilot.get("commission_round_turn_per_lot", 7.0))
    risk_per_lot = abs(float(one_lot_pnl)) + commission
    fixed_volume = pilot.get("fixed_lot_size")
    risk_budget = float(account.equity) * float(pilot.get("risk_per_trade_pct", 0.10)) / 100.0
    raw_volume = float(fixed_volume) if fixed_volume is not None else risk_budget / risk_per_lot
    volume_step = float(symbol_info.volume_step)
    minimum = float(symbol_info.volume_min)
    maximum = min(
        float(symbol_info.volume_max),
        float(lots.get("max_lot_per_trade", 0.50)),
        float(pilot.get("max_lot_per_trade", 0.50)),
    )
    if volume_step <= 0 or minimum <= 0 or maximum < minimum:
        return None, 0.0
    volume = math.floor(min(raw_volume, maximum) / volume_step + 1e-9) * volume_step
    volume = round(volume, 8)
    if volume < minimum or (fixed_volume is not None and (
        float(fixed_volume) > maximum + 1e-9 or abs(volume - float(fixed_volume)) > 1e-9
    )):
        return None, 0.0
    actual_risk = risk_per_lot * volume
    if fixed_volume is None and actual_risk > risk_budget * 1.001:
        return None, actual_risk

    filling = _filling_mode(mt5, symbol_info)
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": entry,
        "sl": stop,
        "tp": target,
        "deviation": int(mt5_config.get("deviation_points", 20)),
        "magic": int(mt5_config.get("magic_number", 20260101)),
        "comment": "causal_demo_pilot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    return request, actual_risk


def _money_risk_stop_price(
    mt5: Any,
    symbol: str,
    symbol_info: Any,
    tick: Any,
    side: str,
    volume: float,
    maximum_loss: float,
    pilot: dict,
    mt5_config: dict,
) -> float | None:
    """Find an attached stop whose modeled loss, costs included, fits a cash cap."""
    if volume <= 0 or maximum_loss <= 0:
        return None
    is_buy = side == "BUY"
    order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
    entry = float(tick.ask if is_buy else tick.bid)
    point = float(symbol_info.point)
    scale = 10 ** int(symbol_info.digits)
    spread = float(tick.ask) - float(tick.bid)
    broker_stop_distance = float(getattr(symbol_info, "trade_stops_level", 0)) * point
    minimum_distance = max(point, spread + broker_stop_distance + point)
    slippage = max(
        float(pilot.get("assumed_slippage_price_units_per_side", 0.10)),
        int(mt5_config.get("deviation_points", 20)) * point,
    )
    commission = float(pilot.get("commission_round_turn_per_lot", 7.0)) * volume

    def candidate(distance: float) -> float:
        raw = entry - distance if is_buy else entry + distance
        return math.ceil(raw * scale) / scale if is_buy else math.floor(raw * scale) / scale

    def modeled_loss(stop: float) -> float:
        worst_entry = entry + slippage if is_buy else entry - slippage
        worst_stop = stop - slippage if is_buy else stop + slippage
        pnl = mt5.order_calc_profit(order_type, symbol, volume, worst_entry, worst_stop)
        if pnl is None or not math.isfinite(float(pnl)):
            return math.inf
        return max(0.0, -float(pnl)) + commission

    lower = minimum_distance
    lower_stop = candidate(lower)
    if modeled_loss(lower_stop) > maximum_loss:
        return None
    upper = max(lower * 2.0, point * 2.0)
    for _ in range(32):
        if modeled_loss(candidate(upper)) > maximum_loss:
            break
        lower, upper = upper, upper * 2.0
    else:
        return None
    for _ in range(64):
        middle = (lower + upper) / 2.0
        if modeled_loss(candidate(middle)) <= maximum_loss:
            lower = middle
        else:
            upper = middle
    stop = candidate(lower)
    stop_level = broker_stop_distance
    if is_buy and float(tick.bid) - stop < stop_level:
        return None
    if not is_buy and stop - float(tick.ask) < stop_level:
        return None
    if modeled_loss(stop) > maximum_loss:
        return None
    return stop


def _batch_target_price(
    mt5: Any,
    symbol: str,
    side: str,
    tick: Any,
    symbol_info: Any,
    lot_per_entry: float,
    batch_size: int,
    net_target: float,
    pilot: dict,
    mt5_config: dict,
) -> float | None:
    """Find a price target whose modeled batch net is the requested amount."""
    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    entry = float(tick.ask if side == "BUY" else tick.bid)
    slippage = max(
        float(pilot.get("assumed_slippage_price_units_per_side", 0.10)),
        int(mt5_config.get("deviation_points", 20)) * float(symbol_info.point),
    )
    per_entry_net = float(net_target) / max(1, int(batch_size))
    commission = float(pilot.get("commission_round_turn_per_lot", 7.0)) * lot_per_entry
    lo, hi = 0.0, 100.0
    for _ in range(48):
        distance = (lo + hi) / 2
        if side == "BUY":
            worst_entry, worst_exit = entry + slippage, entry + distance - slippage
        else:
            worst_entry, worst_exit = entry - slippage, entry - distance + slippage
        gross = mt5.order_calc_profit(order_type, symbol, lot_per_entry, worst_entry, worst_exit)
        if gross is None or not math.isfinite(float(gross)):
            return None
        if float(gross) - commission >= per_entry_net:
            hi = distance
        else:
            lo = distance
    scale = 10 ** int(symbol_info.digits)
    target = entry + hi if side == "BUY" else entry - hi
    target = math.ceil(target * scale) / scale if side == "BUY" else math.floor(target * scale) / scale
    return target if target > 0 else None


@contextmanager
def _account_order_lock(path: Path, timeout_seconds: float = 15.0):
    """Serialize orders with an OS lock, which is safe across Windows TF workers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    try:
        if os.name == "nt":
            import msvcrt

            while time.monotonic() < deadline:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl

            while time.monotonic() < deadline:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    time.sleep(0.05)
        if not acquired:
            raise DemoSafetyError(f"Timed out waiting for account order lock: {path}")
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _remove_dead_lock(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = int(payload.get("pid", -1))
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            process = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if process:
                kernel32.CloseHandle(process)
                return False
            if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER means this PID is not running.
                path.unlink(missing_ok=True)
                return True
            return False
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        path.unlink(missing_ok=True)
        return True
    except PermissionError:
        return False
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _flatten_account_positions(mt5: Any, mt5_config: dict, log_path: Path | None = None) -> None:
    """Manual KILL action: attempt to close every open position on the account."""
    for position in _positions(mt5):
        symbol = str(position.symbol)
        tick = mt5.symbol_info_tick(symbol)
        symbol_info = mt5.symbol_info(symbol)
        if tick is None or symbol_info is None:
            _emit({"event": "kill_flatten_failed", "ticket": int(position.ticket),
                   "reason": "symbol_quote_or_spec_unavailable"}, log_path)
            continue
        is_buy = int(position.type) == int(mt5.POSITION_TYPE_BUY)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "position": int(position.ticket),
            "volume": float(position.volume),
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "price": float(tick.bid if is_buy else tick.ask),
            "deviation": int(mt5_config.get("deviation_points", 20)),
            "magic": int(mt5_config.get("magic_number", 20260101)),
            "comment": "manual_kill_flatten",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": _filling_mode(mt5, symbol_info),
        }
        check = mt5.order_check(request)
        if check is None or int(getattr(check, "retcode", -1)) != 0:
            _emit({"event": "kill_flatten_failed", "ticket": int(position.ticket),
                   "reason": "precheck_rejected", "comment": getattr(check, "comment", "no response")}, log_path)
            continue
        result = mt5.order_send(request)
        accepted = result is not None and int(getattr(result, "retcode", -1)) in {
            int(mt5.TRADE_RETCODE_DONE), int(mt5.TRADE_RETCODE_DONE_PARTIAL),
        }
        _emit({"event": "kill_flatten_result", "ticket": int(position.ticket), "accepted": accepted,
               "retcode": getattr(result, "retcode", None), "comment": getattr(result, "comment", "no response")}, log_path)


def _higher_tf_market_context(
    completed_m1_bars: pd.DataFrame,
    timeframe: str,
) -> tuple[int, float, float]:
    """Return latest completed local-timeframe trend, normalized momentum and ATR."""
    if timeframe not in {"M5", "M15"}:
        return 0, 0.0, 0.0
    bars = resample_m1(completed_m1_bars, TIMEFRAMES[timeframe])
    if len(bars) < 40:
        return 0, 0.0, 0.0
    states, _ = structure(bars, timeframe, k=1.0)
    atr = float(states["atr14"].iloc[-1])
    if not math.isfinite(atr) or atr <= 0:
        return 0, 0.0, 0.0
    closes = bars["close"].astype(float)
    momentum = float((closes.iloc[-1] - closes.iloc[-6]) / atr)
    return int(states["trend"].iloc[-1]), momentum, atr


def _manage_higher_tf_positions(
    mt5: Any,
    symbol: str,
    positions: tuple[Any, ...],
    mt5_config: dict,
    tick: Any,
    attempted: set[int],
    timeframe: str,
    pilot: dict,
    completed_m1_bars: pd.DataFrame,
    deal_cursor: dict[str, Any] | None,
    cursor_path: Path,
    log_path: Path | None = None,
) -> set[int]:
    """Protect aligned M5/M15 winners and extend targets while trend pressure holds."""
    if timeframe not in {"M5", "M15"}:
        return attempted
    enabled = bool(pilot.get("hold_for_extension", {}).get("enabled", True))
    if not enabled:
        return attempted
    trend, momentum, atr = _higher_tf_market_context(completed_m1_bars, timeframe)
    if atr <= 0:
        return attempted
    magic = int(mt5_config.get("magic_number", 20260101))
    symbol_info = mt5.symbol_info(symbol)
    point = float(getattr(symbol_info, "point", 0.0) or 0.0)
    stops_level = int(getattr(symbol_info, "trade_stops_level", 0) or 0)
    trail_config = pilot.get("profit_management", {})
    start_r = float(trail_config.get("trail_start_r", 0.80))
    atr_multiple = float(trail_config.get("atr_trail_multiple", 1.20))
    trail_gap_r = float(trail_config.get("trail_gap_r", 0.50))
    extension_atr = float(trail_config.get("target_extension_atr", 1.50))
    contexts = (deal_cursor or {}).setdefault("open_positions", {})
    changed = False

    for position in positions:
        if int(getattr(position, "magic", -1)) != magic or int(position.ticket) in attempted:
            continue
        is_buy = int(position.type) == int(mt5.POSITION_TYPE_BUY)
        direction = 1 if is_buy else -1
        entry = float(position.price_open)
        current_sl = float(getattr(position, "sl", 0.0))
        current_tp = float(getattr(position, "tp", 0.0))
        if current_sl <= 0:
            _emit({"event": "higher_tf_position_unprotected", "ticket": int(position.ticket),
                   "timeframe": timeframe, "new_orders": "blocked_until_resolved"}, log_path)
            continue
        mark = float(tick.bid if is_buy else tick.ask)
        favorable = direction * (mark - entry)
        context = contexts.get(str(int(getattr(position, "identifier", position.ticket))), {})
        initial_risk = float(context.get("initial_risk_distance", abs(entry - current_sl)))
        if initial_risk <= 0:
            initial_risk = abs(entry - current_sl)
        if initial_risk <= 0:
            continue
        r_multiple = favorable / initial_risk
        context["peak_r"] = max(float(context.get("peak_r", 0.0)), r_multiple)
        contexts[str(int(getattr(position, "identifier", position.ticket)))] = context
        changed = True
        trend_aligned = trend == direction
        momentum_aligned = direction * momentum >= 0.10

        if favorable > 0 and trend == -direction and direction * momentum < -0.15:
            _emit({"event": "higher_tf_structure_reversal_exit", "ticket": int(position.ticket),
                   "timeframe": timeframe, "r_multiple": round(r_multiple, 3),
                   "trend": trend, "momentum_atr": round(momentum, 3)}, log_path)
            attempted = _close_pilot_positions(
                mt5, symbol, (position,), mt5_config, tick, attempted,
                "higher_tf_structure_reversal_exit", log_path,
            )
            continue

        if r_multiple < start_r or not (trend_aligned and momentum_aligned):
            continue
        minimum_gap = max(stops_level * point, point)
        trailing_gap = min(atr * atr_multiple, initial_risk * trail_gap_r)
        proposed_sl = mark - direction * trailing_gap
        if is_buy:
            proposed_sl = min(proposed_sl, float(tick.bid) - minimum_gap)
            improved_sl = proposed_sl > current_sl + point / 2
        else:
            proposed_sl = max(proposed_sl, float(tick.ask) + minimum_gap)
            improved_sl = proposed_sl < current_sl - point / 2

        proposed_tp = current_tp
        near_target = (
            current_tp > 0
            and direction * (current_tp - mark) <= 0.75 * atr
            and direction * (current_tp - mark) > 0
        )
        if near_target:
            extended = mark + direction * extension_atr * atr
            if current_tp <= 0 or direction * (extended - current_tp) > point:
                proposed_tp = extended
        if not improved_sl and proposed_tp == current_tp:
            continue
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "position": int(position.ticket),
            "sl": float(proposed_sl if improved_sl else current_sl),
            "tp": float(proposed_tp),
            "magic": magic,
            "comment": f"{timeframe.lower()}_profit_protect"[:31],
        }
        result = mt5.order_send(request)
        accepted = result is not None and int(getattr(result, "retcode", -1)) == int(mt5.TRADE_RETCODE_DONE)
        _emit({
            "event": "higher_tf_profit_management",
            "ticket": int(position.ticket),
            "timeframe": timeframe,
            "accepted": accepted,
            "retcode": getattr(result, "retcode", None),
            "r_multiple": round(r_multiple, 3),
            "trend_aligned": trend_aligned,
            "momentum_atr": round(momentum, 3),
            "old_sl": current_sl,
            "new_sl": request["sl"],
            "old_tp": current_tp,
            "new_tp": request["tp"],
            "stop_action": "tighten_only",
        }, log_path)
        if accepted:
            context["stop_loss"] = request["sl"]
            context["take_profit"] = request["tp"]
            context["last_profit_management_utc"] = datetime.now(timezone.utc).isoformat()
            changed = True
    if changed and deal_cursor is not None:
        _save_deal_cursor(cursor_path, deal_cursor)
    return attempted


def _close_timed_out_positions(
    mt5: Any,
    symbol: str,
    positions: tuple[Any, ...],
    mt5_config: dict,
    tick: Any,
    attempted: set[int],
    timeframe: str,
    pilot: dict,
    completed_m1_bars: pd.DataFrame,
    log_path: Path | None = None,
) -> set[int]:
    limits = pilot.get("max_hold_m1_bars", {"M1": 15, "M5": 30, "M15": 60})
    maximum_bars = int(limits.get(timeframe, 60))
    if maximum_bars < 1:
        raise DemoSafetyError(f"max_hold_m1_bars[{timeframe}] must be at least 1")
    completed = completed_m1_bars.index + pd.Timedelta(minutes=1)
    timed_out = []
    for position in positions:
        if int(getattr(position, "magic", -1)) != int(mt5_config.get("magic_number", 20260101)):
            continue
        opened_at = pd.to_datetime(float(position.time), unit="s", utc=True)
        elapsed_bars = int((completed > opened_at).sum())
        if elapsed_bars >= maximum_bars:
            if timeframe in {"M5", "M15"}:
                is_buy = int(position.type) == int(mt5.POSITION_TYPE_BUY)
                direction = 1 if is_buy else -1
                mark = float(tick.bid if is_buy else tick.ask)
                if direction * (mark - float(position.price_open)) > 0:
                    trend, momentum, _ = _higher_tf_market_context(completed_m1_bars, timeframe)
                    if trend == direction and direction * momentum >= 0.10:
                        continue
            timed_out.append(position)
    return _close_pilot_positions(mt5, symbol, timed_out, mt5_config, tick, attempted, "time_limit_exit", log_path)


def _close_pilot_positions(
    mt5: Any,
    symbol: str,
    positions: tuple[Any, ...],
    mt5_config: dict,
    tick: Any,
    attempted: set[int],
    reason: str,
    log_path: Path | None = None,
) -> set[int]:
    magic = int(mt5_config.get("magic_number", 20260101))
    for position in positions:
        if int(getattr(position, "magic", -1)) != magic:
            continue
        ticket = int(position.ticket)
        if ticket in attempted:
            continue
        is_buy = int(position.type) == int(mt5.POSITION_TYPE_BUY)
        close_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "position": int(position.ticket),
            "volume": float(position.volume),
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "price": float(tick.bid if is_buy else tick.ask),
            "deviation": int(mt5_config.get("deviation_points", 20)),
            "magic": magic,
            "comment": f"pilot_{reason}"[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": _filling_mode(mt5, mt5.symbol_info(symbol)),
        }
        check = mt5.order_check(close_request)
        if check is None or int(getattr(check, "retcode", -1)) != 0:
            _emit({"event": "position_close_precheck_rejected", "reason": reason, "ticket": ticket,
                   "retcode": getattr(check, "retcode", None), "comment": getattr(check, "comment", "no response")}, log_path)
            continue
        result = mt5.order_send(close_request)
        accepted = {int(mt5.TRADE_RETCODE_DONE), int(mt5.TRADE_RETCODE_DONE_PARTIAL)}
        _emit({
            "event": "position_close_result",
            "reason": reason,
            "ticket": ticket,
            "accepted": bool(result is not None and int(getattr(result, "retcode", -1)) in accepted),
            "retcode": getattr(result, "retcode", None),
            "comment": getattr(result, "comment", "no response"),
        }, log_path)
        if result is not None and int(getattr(result, "retcode", -1)) == int(mt5.TRADE_RETCODE_DONE):
            attempted.add(ticket)
    return attempted


def _filling_mode(mt5: Any, symbol_info: Any) -> int:
    mode = int(getattr(symbol_info, "filling_mode", 0))
    if mode & int(getattr(mt5, "SYMBOL_FILLING_FOK", 1)):
        return int(mt5.ORDER_FILLING_FOK)
    if mode & int(getattr(mt5, "SYMBOL_FILLING_IOC", 2)):
        return int(mt5.ORDER_FILLING_IOC)
    return int(mt5.ORDER_FILLING_RETURN)
