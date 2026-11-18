"""One MT5 client, one account lock, independent symbol engines. DEMO orders only."""

from __future__ import annotations

import hashlib
import json
import math
import time
from contextlib import nullcontext
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from bot.execution.demo_runner import (
    DemoSafetyError,
    _account_order_lock,
    _filling_mode,
    _side_is_allowed,
)

from .config import ZoneConfig
from .engine import Bar, ZoneEngine
from .hours import Hours
from .orders import EquityGuard, plan_entry, portfolio_block, reversal


def account_check(mt, mode, armed, identity=None):
    a, terminal = mt.account_info(), mt.terminal_info()
    if a is None or terminal is None or not terminal.connected:
        raise DemoSafetyError("Account / terminal disconnected; no orders")
    if identity is not None and identity != (int(a.login), str(a.server)):
        raise DemoSafetyError("Account changed; restart and reconcile")
    if armed and (
        mode != "demo"
        or a.trade_mode != mt.ACCOUNT_TRADE_MODE_DEMO
        or a.currency != "USD"
        or a.margin_mode != mt.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
        or not a.trade_allowed
        or not a.trade_expert
        or not terminal.trade_allowed
    ):
        raise DemoSafetyError(
            "Arming requires explicit demo mode, a USD hedging DEMO account and trading permission"
        )
    return a


def fresh(tick, now, max_age):
    if tick is None:
        return False
    ts = float(getattr(tick, "time_msc", 0)) / 1000
    return (
        all(math.isfinite(v) for v in (ts, tick.bid, tick.ask))
        and 0 < tick.bid <= tick.ask
        and 0 <= now - ts <= max_age
    )


def check_spec(s, info):
    if info is None:
        return "unknown_instrument"
    actual = (info.point, info.trade_tick_size, info.trade_contract_size)
    if not all(
        math.isclose(a, b, rel_tol=1e-8)
        for a, b in zip(actual, (s.point, s.tick_size, s.contract_size))
    ):
        return "broker_contract_point_or_tick_mismatch"
    if (s.conversion == "quote_usd" and info.currency_profit != "USD") or (
        s.conversion == "base_usd" and info.currency_base != "USD"
    ):
        return "unsupported_profit_currency"
    if not (
        info.volume_min <= s.lots <= info.volume_max
        and info.volume_step > 0
        and math.isclose(
            s.lots / info.volume_step, round(s.lots / info.volume_step), abs_tol=1e-7
        )
    ):
        return "invalid_lot_no_fallback"
    return ""


def snapshot(mt):
    ps, os = mt.positions_get(), mt.orders_get()
    if ps is None or os is None:
        raise DemoSafetyError("Broker exposure unavailable; fail closed")
    return ps, os


def _owned(p, symbols, magic):
    return p.symbol in symbols and p.magic == magic


def used_risk(mt, positions, symbols, magic):
    risk = 0.0
    for p in positions:
        if not _owned(p, symbols, magic) or p.sl <= 0 or p.tp <= 0:
            return None  # don't place experimental trades beside unknown exposure
        s = symbols[p.symbol]
        side = 1 if p.type == mt.POSITION_TYPE_BUY else -1
        typ = mt.ORDER_TYPE_BUY if side == 1 else mt.ORDER_TYPE_SELL
        gross = mt.order_calc_profit(
            typ, p.symbol, p.volume, p.price_open, p.sl - side * s.slippage
        )
        if gross is None or not math.isfinite(gross):
            return None
        risk += max(s.sl_usd, -gross + p.volume * s.commission)
    return risk


def save(path, state):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


class Desk:
    def __init__(self, mt, cfg, z, identity, state, path, emit):
        self.mt, self.cfg, self.z, self.identity = mt, cfg, z, identity
        self.symbols = {s.broker_symbol: s for s in z.symbols.values() if s.enabled}
        self.state, self.path, self.emit = state, path, emit
        self.guard = EquityGuard(**state.get("guard", {}))
        self.history_ok = False
        self.mode = cfg["run"]["mode"]
        self.lim = z.limits
        self.max_age = float(cfg["safety"]["stale_tick_seconds"])
        self.kill = Path(cfg["safety"]["emergency_flatten_hotkey_file"])
        self.hours = Hours(cfg)

    def mark(self, a, now):
        reason = self.guard.mark(
            datetime.fromtimestamp(now, UTC).date().isoformat(),
            float(a.equity),
            self.lim,
        )
        g = asdict(self.guard)
        if self.state.get("guard") != g:
            self.state["guard"] = g
            save(self.path, self.state)
        return reason

    def sync_deals(self, now):
        # Read-only audit of individual entry and exit deals (including entry commission).
        mt = self.mt
        cursor = self.state.get("deals_since", int(now) - 3600)
        ds = mt.history_deals_get(
            datetime.fromtimestamp(cursor, UTC),
            datetime.fromtimestamp(now + 1, UTC),
        )
        self.history_ok = ds is not None
        if not self.history_ok:
            self.emit("history_unavailable", {})
            return
        seen = self.state.get("seen", {})
        floor = int(now) - 300
        for d in ds:
            if _owned(d, self.symbols, self.lim.magic) and str(d.ticket) not in seen:
                self.emit(
                    "broker_deal",
                    {
                        k: getattr(d, k, None)
                        for k in (
                            "ticket",
                            "position_id",
                            "symbol",
                            "time_msc",
                            "entry",
                            "volume",
                            "price",
                            "profit",
                            "commission",
                            "fee",
                            "swap",
                            "comment",
                        )
                    },
                )
                seen[str(d.ticket)] = int(d.time)
        self.state.update(
            deals_since=floor, seen={k: v for k, v in seen.items() if v >= floor}
        )
        save(self.path, self.state)

    def close(self, p, reason):
        mt = self.mt
        if not _owned(p, self.symbols, self.lim.magic):
            return
        account_check(mt, self.mode, True, self.identity)
        active = mt.positions_get(ticket=p.ticket)
        if active is None:
            raise DemoSafetyError("Cannot verify position before close")
        if not active or not _owned(active[0], self.symbols, self.lim.magic):
            return
        p = active[0]
        tick = mt.symbol_info_tick(p.symbol)
        info = mt.symbol_info(p.symbol)
        if not fresh(tick, time.time(), self.max_age) or info is None:
            return
        buy = p.type != mt.POSITION_TYPE_BUY
        s = self.symbols[p.symbol]
        req = {
            "action": mt.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "position": p.ticket,
            "volume": p.volume,
            "type": mt.ORDER_TYPE_BUY if buy else mt.ORDER_TYPE_SELL,
            "price": tick.ask if buy else tick.bid,
            "magic": self.lim.magic,
            "deviation": int(s.slippage / info.point + 1e-9),
            "type_filling": _filling_mode(mt, info),
            "type_time": mt.ORDER_TIME_GTC,
            "comment": "zones_exit",
        }
        account_check(mt, self.mode, True, self.identity)
        res = mt.order_send(req)
        self.emit(
            "exit_request",
            {
                "symbol": p.symbol,
                "ticket": p.ticket,
                "reason": reason,
                "retcode": getattr(res, "retcode", None),
            },
        )

    def send(self, s, d, *, watch=None):
        """Durable intent BEFORE first send. Partial batch = keep protected fills, never top up/retry."""
        mt = self.mt
        name = s.broker_symbol
        if self.z.price_action.policy != "baseline" and watch is None:
            return "frozen_watch_required"
        if watch is not None:
            from .price_action import return_plan, rules_for
            from .quote_path import path_cancel, quotes_to_snapshot

            if (
                not self.z.price_action.demo_opt_in
                or watch.d != d
                or watch.policy != self.z.price_action.policy
            ):
                return "challenger_not_armed"
        deadline = (
            watch.until_ms / 1000
            if watch is not None
            else d.t + self.lim.entry_age_seconds
        )
        if self.state.get("last_attempt", {}).get(name, 0) >= d.t:
            return "already_attempted"
        if self.state.get("manual_hold"):
            return "reconcile_uncertain_send"
        if not self.history_ok:
            return "history_unavailable"
        seen_tickets = set()
        for leg in range(s.legs):
            a = account_check(mt, self.mode, True, self.identity)
            now = time.time()
            block = self.mark(a, now) or self.hours.status(now)[0]
            if block:
                return block
            if self.kill.exists():
                return "kill"
            recent = [ts for ts in self.state.get("entry_sends", []) if ts > now - 60]
            if len(recent) + s.legs - leg > self.cfg["safety"]["max_orders_per_minute"]:
                return "order_rate_limit"
            ps, os = snapshot(mt)
            if os:
                return "pending_orders_present"
            same = [p for p in ps if p.symbol == name]
            if len(same) != leg or any(
                not _owned(p, self.symbols, self.lim.magic) for p in same
            ):
                return "symbol_busy_or_batch_not_reconciled"
            current = {p.ticket for p in same}
            if not seen_tickets <= current:
                return "batch_changed"
            seen_tickets = current
            risk = used_risk(mt, ps, self.symbols, self.lim.magic)
            if risk is None:
                return "foreign_or_unprotected_exposure"
            remaining = s.model_copy(update={"legs": s.legs - leg})
            block = portfolio_block(
                remaining, self.lim, len(ps), sum(p.volume for p in ps), risk
            )
            if block:
                return block
            info = mt.symbol_info(name)
            tick = mt.symbol_info_tick(name)
            block = check_spec(s, info)
            if block:
                return block
            if not _side_is_allowed(mt, info, "BUY" if d.side == 1 else "SELL"):
                return "side_disabled"
            now = time.time()
            if (
                not fresh(tick, now, self.max_age)
                or not d.t <= tick.time_msc / 1000 <= now <= deadline
            ):
                return "stale_signal_or_quote"
            min_dist = max(info.trade_stops_level, info.trade_freeze_level) * info.point
            if watch is not None:
                p, block = return_plan(
                    watch,
                    s,
                    rules_for(self.z.rules, watch.policy),
                    tick.bid,
                    tick.ask,
                    tick.time_msc,
                    min_dist,
                )
            else:
                p, block = plan_entry(d, s, self.z.rules, tick.bid, tick.ask, min_dist)
            if not p:
                return block
            typ = mt.ORDER_TYPE_BUY if d.side == 1 else mt.ORDER_TYPE_SELL
            gross = mt.order_calc_profit(
                typ, name, s.lots, p.modeled_fill, p.sl - d.side * s.slippage
            )
            reward = mt.order_calc_profit(
                typ, name, s.lots, p.modeled_fill, p.tp - d.side * s.slippage
            )
            fee = s.lots * s.commission
            if (
                gross is None
                or reward is None
                or not all(math.isfinite(x) for x in (gross, reward))
                or (fee - gross > s.sl_usd + 1e-7 or reward - fee < s.tp_usd - 1e-7)
            ):
                return "broker_cash_conversion_mismatch"
            request = {
                "action": mt.TRADE_ACTION_DEAL,
                "symbol": name,
                "type": typ,
                "volume": s.lots,
                "price": p.requested,
                "sl": p.sl,
                "tp": p.tp,
                "deviation": int(s.slippage / info.point + 1e-9),
                "magic": self.lim.magic,
                "type_filling": _filling_mode(mt, info),
                "type_time": mt.ORDER_TIME_GTC,
                "comment": f"z{d.tf}_{d.t}_{leg + 1}",
            }
            check = mt.order_check(request)
            if check is None or check.retcode != 0:
                return "broker_precheck_rejected"
            if (
                a.equity <= 0
                or not math.isfinite(check.margin_free)
                or 100 * check.margin_free / a.equity
                < self.cfg["risk"]["min_free_margin_pct"]
            ):
                return "margin_floor"
            if leg == 0:
                self.state.setdefault("last_attempt", {})[name] = d.t
            # An interruption between send and acknowledgement requires manual reconciliation.
            self.state["manual_hold"] = {"symbol": name, "signal": d.t, "leg": leg + 1}
            save(self.path, self.state)
            account_check(mt, self.mode, True, self.identity)
            q2 = mt.symbol_info_tick(name)
            now = time.time()
            verified = True
            if watch is not None and q2 is not None:
                path = quotes_to_snapshot(mt, name, d.t * 1000, q2)
                verified = path is not None and not path_cancel(watch, path[1])
                # Do not send against an older snapshot after the history query.
                q3 = mt.symbol_info_tick(name)
                verified = (
                    verified
                    and q3 is not None
                    and (q2.time_msc, q2.bid, q2.ask) == (q3.time_msc, q3.bid, q3.ask)
                )
            now = time.time()
            if (
                not verified
                or self.kill.exists()
                or self.hours.status(now)[0]
                or now >= deadline
                or not fresh(q2, now, self.max_age)
                or (q2.bid, q2.ask) != (tick.bid, tick.ask)
            ):
                self.state.pop("manual_hold")
                save(self.path, self.state)
                return "final_quote_or_safety_changed"
            self.state["entry_sends"] = [*recent, now]
            save(self.path, self.state)
            res = mt.order_send(request)
            self.emit(
                "order_result",
                {
                    "symbol": name,
                    "leg": leg + 1,
                    "signal_close": d.t,
                    "plan": asdict(p),
                    "request": request,
                    "retcode": getattr(res, "retcode", None),
                    "deal": getattr(res, "deal", None),
                    "price": getattr(res, "price", None),
                    "volume": getattr(res, "volume", None),
                },
            )
            if (
                res is None
                or res.retcode != mt.TRADE_RETCODE_DONE
                or not math.isclose(res.volume, s.lots, abs_tol=1e-8)
            ):
                return "incomplete_or_unverified_batch_manual_hold"
            if watch is not None:
                actual = float(getattr(res, "price", 0) or 0)
                gross = (
                    mt.order_calc_profit(
                        typ, name, s.lots, actual, p.sl - d.side * s.slippage
                    )
                    if actual > 0
                    else None
                )
                if (
                    gross is None
                    or not math.isfinite(gross)
                    or fee - gross > s.sl_usd + 1e-7
                ):
                    self.emit(
                        "fill_risk_mismatch",
                        {
                            "symbol": name,
                            "signal": d.t,
                            "actual_fill": actual,
                            "cap": s.sl_usd,
                        },
                    )
                    new = [
                        x
                        for x in snapshot(mt)[0]
                        if x.symbol == name
                        and x.magic == self.lim.magic
                        and x.ticket not in current
                    ]
                    if len(new) == 1:
                        self.close(new[0], "fill_risk_mismatch")
                    return "broker_fill_risk_manual_hold"
            self.state.pop("manual_hold")
            save(self.path, self.state)
        return "demo_batch_sent"


def monitor(cfg, armed=False, poll=None, iterations=None):
    z = ZoneConfig.model_validate(cfg["zones"])
    if poll is None:
        poll = z.price_action.poll_seconds
    if not 0.5 <= poll <= 10 or (iterations is not None and iterations < 1):
        raise ValueError("Invalid polling limits")
    if armed and z.price_action.policy != "baseline" and not z.price_action.demo_opt_in:
        raise DemoSafetyError(
            "Candle challenger is shadow-only unless price_action.demo_opt_in is explicit"
        )
    if armed and cfg["run"]["mode"] != "demo":
        raise DemoSafetyError("Use explicit run.mode: demo; paper cannot send orders")
    try:
        import MetaTrader5 as mt
    except ImportError as exc:
        raise DemoSafetyError(
            "Install the live extra on Windows with an MT5 demo terminal"
        ) from exc
    path = cfg.get("mt5", {}).get("terminal_path", "")
    if not (mt.initialize(path) if path else mt.initialize()):
        raise DemoSafetyError(f"MT5 initialization failed: {mt.last_error()}")
    try:
        a = account_check(mt, cfg["run"]["mode"], armed)
        identity = (int(a.login), str(a.server))
        key = hashlib.sha256(repr(identity).encode()).hexdigest()[:16]
        root = Path("reports/zone_runtime") / key
        root.mkdir(parents=True, exist_ok=True)

        def emit(event, fields):
            payload = json.dumps(
                dict(
                    event=event,
                    logged_at=datetime.now(UTC).isoformat(),
                    **fields,
                ),
                allow_nan=False,
            )
            with (root / ("demo.jsonl" if armed else "shadow.jsonl")).open(
                "a", encoding="utf-8"
            ) as f:
                print(payload, file=f)
            if event != "decision" or fields.get("side"):
                print(payload, flush=True)

        with _account_order_lock(root / "scanner.lock", 1) if armed else nullcontext():
            state_path = root / "state.json"
            state = (
                json.loads(state_path.read_text())
                if armed and state_path.exists()
                else {}
            )
            desk = Desk(mt, cfg, z, identity, state, state_path, emit)
            from .watch_monitor import SymbolWatch

            engines = {}
            watches = {}
            history = max(2400, int(cfg["pilot"]["history_m1_bars"]))
            for name, s in desk.symbols.items():
                if not mt.symbol_select(name, True):
                    raise DemoSafetyError(f"Cannot select configured symbol: {name}")
                mismatch = check_spec(s, mt.symbol_info(name))
                if mismatch:
                    raise DemoSafetyError(
                        f"{name}: {mismatch}; verify profile in config.yaml"
                    )
                engines[name] = ZoneEngine(z.rules, s)
                if z.price_action.policy != "baseline":
                    watches[name] = SymbolWatch(s, z)
            emit(
                "scanner_started",
                {
                    "armed": armed,
                    "symbols": list(engines),
                    "config_hash": hashlib.sha256(
                        json.dumps(cfg, sort_keys=True).encode()
                    ).hexdigest(),
                },
            )
            i = 0
            last_deal_sync = 0
            while iterations is None or i < iterations:
                i += 1
                now = time.time()
                a = account_check(mt, cfg["run"]["mode"], armed, identity)
                if armed:
                    desk.mark(a, now)
                    if now - last_deal_sync >= 30:
                        desk.sync_deals(now)
                        last_deal_sync = now
                    positions, _ = snapshot(mt)
                else:
                    positions = []
                for name, engine in engines.items():
                    own = [
                        p
                        for p in positions
                        if _owned(p, desk.symbols, z.limits.magic) and p.symbol == name
                    ]
                    halt, flatten = desk.hours.status(time.time())
                    killed = desk.kill.exists()
                    if armed and not killed and not flatten:
                        for p in own:
                            if p.sl <= 0 or p.tp <= 0:
                                desk.close(p, "missing_broker_protection")
                    if armed and (killed or flatten):
                        for p in own:
                            desk.close(p, "kill" if killed else "scheduled_break")
                    cold = engine.prev is None
                    count = history if cold or now - engine.prev.t > 180 else 3
                    rates = mt.copy_rates_from_pos(
                        name, mt.TIMEFRAME_M1, 1, count
                    )  # NEVER use forming index zero
                    if rates is None or len(rates) == 0:
                        if name in watches:
                            watches[name].poll(
                                desk,
                                None,
                                cold,
                                True,
                                False,
                                own,
                                armed,
                                halt,
                                flatten,
                                killed,
                            )
                        emit("data_unavailable", {"symbol": name})
                        continue
                    d = None
                    before = engine.prev.t if engine.prev else None
                    for x in rates:
                        t = int(x["time"])
                        if t + 60 <= time.time() and (
                            engine.prev is None or t > engine.prev.t
                        ):
                            d = engine.update(
                                Bar(
                                    t,
                                    *[
                                        float(x[k])
                                        for k in ("open", "high", "low", "close")
                                    ],
                                )
                            )
                    if name in watches:
                        expected = int(time.time()) // 60 * 60
                        good = (
                            engine.prev is not None and engine.prev.t + 60 == expected
                        )
                        gap = bool(
                            before is not None
                            and d is not None
                            and engine.prev.t != before + 60
                        )
                        watches[name].poll(
                            desk, d, cold, gap, good, own, armed, halt, flatten, killed
                        )
                        continue
                    if d is None:
                        continue
                    s = desk.symbols[name]
                    q = mt.symbol_info_tick(name)
                    stamp = time.time()
                    if (
                        armed
                        and not killed
                        and not flatten
                        and not cold
                        and stamp - d.t < 60
                    ):
                        for p in own:
                            if reversal(
                                d, 1 if p.type == mt.POSITION_TYPE_BUY else -1, z.rules
                            ):
                                desk.close(p, "adverse_structure")
                    outcome = (
                        "startup_observe"
                        if cold
                        else "kill"
                        if killed
                        else halt or ("symbol_busy" if own else "")
                    )
                    plan = None
                    if not outcome and d.side:
                        if (
                            not fresh(q, stamp, desk.max_age)
                            or not d.t
                            <= q.time_msc / 1000
                            <= stamp
                            <= d.t + z.limits.entry_age_seconds
                        ):
                            outcome = "stale_signal_or_quote"
                        else:
                            info = mt.symbol_info(name)
                            if info is None:
                                outcome = "symbol_info_unavailable"
                            else:
                                plan, outcome = plan_entry(
                                    d,
                                    s,
                                    z.rules,
                                    q.bid,
                                    q.ask,
                                    max(info.trade_stops_level, info.trade_freeze_level)
                                    * info.point,
                                )
                            if plan:
                                outcome = (
                                    desk.send(s, d) if armed else "shadow_plan_only"
                                )
                    emit(
                        "decision",
                        dict(
                            symbol=name,
                            **asdict(d),
                            outcome=outcome or "observe",
                            plan=asdict(plan) if plan else None,
                        ),
                    )
                if iterations is None or i < iterations:
                    time.sleep(poll)
    finally:
        mt.shutdown()
