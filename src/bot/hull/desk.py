"""Bounded demo batches with durable no-retry intent and broker-side SL and TP on EVERY leg."""

from __future__ import annotations

import math
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from bot.execution.demo_runner import DemoSafetyError, _filling_mode, _side_is_allowed
from bot.zones.hours import Hours
from bot.zones.monitor import account_check, fresh, save, snapshot
from bot.zones.orders import EquityGuard

from .config import COUNTS
from .orders import (
    Block,
    Exposure,
    Grid,
    count_for,
    make_plan,
    room,
    target,
    valid_cash,
)


def resolve(mt, profiles, *, check_lots=True):
    infos = mt.symbols_get()
    if infos is None:
        raise DemoSafetyError("Cannot read broker symbol catalogue")
    names = {v.name for v in infos}
    resolved = {}
    for key, p in profiles.items():
        possibilities = {p.broker_symbol} if p.broker_symbol else {key, *p.aliases}
        found = sorted(names & possibilities)
        if len(found) != 1:
            raise DemoSafetyError(
                f"{key}: exact matches {found}; set hull.symbols.{key}.broker_symbol to one Market Watch name"
            )
        name = found[0]
        if name in resolved or not mt.symbol_select(name, True):
            raise DemoSafetyError(f"{key}: duplicate or unavailable instrument {name}")
        if check_lots:
            Grid.from_info(mt.symbol_info(name)).volume(p.lots)
        resolved[name] = (key, p)
    return resolved


def identity_check(mt, mode, armed, identity=None):
    a = account_check(mt, mode, armed, identity)
    if (
        a.trade_mode != mt.ACCOUNT_TRADE_MODE_DEMO
        or a.currency != "USD"
        or a.margin_mode != mt.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
    ):
        raise DemoSafetyError(
            "Use a dedicated USD hedging DEMO terminal (including for preflight)"
        )
    if not math.isfinite(a.equity) or a.equity <= 0:
        raise DemoSafetyError("Invalid account equity")
    return a


class Desk:
    def __init__(self, mt, cfg, h, symbols, identity, state, path, emit, armed):
        self.mt = mt
        self.h = h
        self.lim = h.limits
        self.syms = symbols
        self.ident = identity
        self.state = state
        self.path = path
        self.emit = emit
        self.armed = armed
        self.mode = cfg["run"]["mode"]
        self.kill = Path(
            cfg.get("safety", {}).get("emergency_flatten_hotkey_file", "KILL")
        )
        self.hours = Hours(cfg)
        self.guard = EquityGuard(**state.get("guard", {}))

    def persist(self):
        if self.armed:
            save(self.path, self.state)

    def account(self):
        return identity_check(self.mt, self.mode, self.armed, self.ident)

    def mark(self, a, now):
        reason = self.guard.mark(
            datetime.fromtimestamp(now, UTC).date().isoformat(), a.equity, self.lim
        )
        g = asdict(self.guard)
        if self.state.get("guard") != g:
            self.state["guard"] = g
            self.persist()
        return reason

    def owned(self, p):
        return p.symbol in self.syms and p.magic == self.h.magic

    def typ(self, d):
        return self.mt.ORDER_TYPE_BUY if d == 1 else self.mt.ORDER_TYPE_SELL

    def cash(self, name):
        return lambda side, lots, entry, px: self.mt.order_calc_profit(
            self.typ(side), name, lots, entry, px
        )

    def exposure(self, ps, os):
        if os:
            raise Block("pending_orders_present")
        risk = 0.0
        for p in ps:
            if not self.owned(p) or not (p.sl > 0 and p.tp > 0):
                raise Block("foreign_or_unprotected_exposure")
            side = 1 if p.type == self.mt.POSITION_TYPE_BUY else -1
            risk += max(
                0.0,
                -valid_cash(self.cash(p.symbol), side, p.volume, p.price_open, p.sl),
            )
        return Exposure(len(ps), sum(p.volume for p in ps), risk)

    def prepare(self, name, d, expected=None, remaining=None):
        mt = self.mt
        p = self.syms[name][1]
        a = self.account()
        now = time.time()
        why = self.mark(a, now) or (
            self.hours.status(now)[0] if p.session == "configured" else ""
        )
        if why:
            raise Block(why)
        if self.kill.exists():
            raise Block("kill")
        if self.state.get("manual_hold"):
            raise Block("manual_reconciliation_required")
        ps, os = snapshot(mt)
        e = self.exposure(ps, os)
        tickets = {r.ticket for r in ps if r.symbol == name}
        if tickets != (expected or set()):
            raise Block("symbol_busy_or_batch_changed")
        i = mt.symbol_info(name)
        grid = Grid.from_info(i)
        if not _side_is_allowed(mt, i, "BUY" if d.side == 1 else "SELL"):
            raise Block("broker_side_disabled")
        q = mt.symbol_info_tick(name)
        now = time.time()
        if (
            not fresh(q, now, self.lim.max_quote_age_s)
            or not d.close_s
            <= q.time_msc / 1000
            <= now
            <= d.close_s + self.lim.max_entry_age_s
        ):
            raise Block("stale_label_or_quote")
        plan = make_plan(d, p, grid, q.bid, q.ask, self.cash(name))
        recent = [v for v in self.state.get("sends", []) if v > now - 60]
        margin = mt.order_calc_margin(self.typ(d.side), name, p.lots, plan.entry)
        if (
            margin is None
            or not math.isfinite(margin)
            or margin < 0
            or not math.isfinite(a.margin_free)
        ):
            raise Block("no_margin_estimate")

        def more(n):
            if len(recent) + n > self.lim.max_orders_minute:
                return "order_rate_limit"
            if (
                100 * (a.margin_free - n * margin) / a.equity
                < self.lim.min_free_margin_pct
            ):
                return "margin_floor"
            return ""

        if remaining is None:
            try:
                n = count_for(p.rate, self.lim, e, plan, more)
            except Block as exc:
                if str(exc) == "portfolio_risk_limit":
                    self.emit(
                        "risk_veto",
                        symbol=name,
                        sl=plan.sl,
                        risk_per_leg=plan.risk_usd,
                        min_legs=min(COUNTS[p.rate]),
                        min_batch_risk=plan.risk_usd * min(COUNTS[p.rate]),
                        open_risk=e.risk,
                        portfolio_cap=self.lim.max_open_risk_usd,
                    )
                raise
        else:
            n = remaining
            why = room(self.lim, e, plan, n) or more(n)
            if why:
                raise Block(why)
        return n, plan, q, i, recent

    def close_owned(self, pos, reason):
        if not self.armed or not self.owned(pos):
            return
        mt = self.mt
        self.account()
        active = mt.positions_get(ticket=pos.ticket)
        if active is None:
            raise DemoSafetyError("Close ticket verification unavailable")
        if (
            len(active) != 1
            or not self.owned(active[0])
            or active[0].symbol != pos.symbol
        ):
            return
        p = active[0]
        q = mt.symbol_info_tick(p.symbol)
        i = mt.symbol_info(p.symbol)
        if i is None or not fresh(q, time.time(), self.lim.max_quote_age_s):
            return
        buy = p.type != mt.POSITION_TYPE_BUY
        req = {
            "action": mt.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "position": p.ticket,
            "volume": p.volume,
            "type": self.typ(1 if buy else -1),
            "price": q.ask if buy else q.bid,
            "deviation": 0,
            "magic": self.h.magic,
            "type_time": mt.ORDER_TIME_GTC,
            "type_filling": _filling_mode(mt, i),
            "comment": "hull_exit",
        }
        self.account()
        res = mt.order_send(req)
        self.emit(
            "exit_request",
            symbol=p.symbol,
            ticket=int(p.ticket),
            reason=reason,
            retcode=getattr(res, "retcode", None),
        )

    def close_opposite(self, name, reading):
        """Close all owned positions against a fresh opposite Hull label."""
        if not self.armed or not reading.side:
            return 0
        ps, _ = snapshot(self.mt)
        closed = 0
        for pos in ps:
            if pos.symbol != name or not self.owned(pos):
                continue
            pos_side = 1 if pos.type == self.mt.POSITION_TYPE_BUY else -1
            if pos_side != reading.side:
                self.close_owned(pos, "opposite_hull_label")
                closed += 1
        if closed:
            self.persist()
        return closed

    def supervise(self):
        if not self.armed:
            return
        ps, _ = snapshot(self.mt)
        goals = self.state.setdefault("target_by_ticket", {})
        active = {
            str(p.ticket) for p in ps
        }  # Never drop a goal solely because a profile was deselected.
        for p in ps:
            if not self.owned(p):
                continue
            goal = goals.get(str(p.ticket))
            why = (
                "kill"
                if self.kill.exists()
                else "missing_protection"
                if not (p.sl > 0 and p.tp > 0)
                else "cash_target"
                if goal and p.profit >= goal
                else ""
            )
            if why:
                self.close_owned(p, why)
        if stale := set(goals) - active:
            for k in stale:
                del goals[k]
            self.persist()

    def finalize(self, name, plan, res, expected):
        mt = self.mt
        prof = self.syms[name][1]
        self.account()
        if (
            res is None
            or res.retcode != mt.TRADE_RETCODE_DONE
            or not math.isclose(float(res.volume), plan.lots, abs_tol=1e-8)
        ):
            return None, "uncertain_or_partial_fill"
        ps, os = snapshot(mt)
        new = [p for p in ps if p.symbol == name and p.ticket not in expected]
        if len(new) != 1 or not self.owned(new[0]):
            return None, "ticket_not_reconciled"
        p = new[0]
        grid = Grid.from_info(mt.symbol_info(name))
        tol = grid.tick * 1e-6
        if (
            p.type != self.typ(plan.side)
            or abs(p.volume - plan.lots) > 1e-8
            or not math.isfinite(p.price_open)
            or p.price_open <= 0
        ):
            return None, "fill_not_reconciled"
        self.state.setdefault("target_by_ticket", {})[str(p.ticket)] = prof.target_usd
        self.persist()
        if abs(p.sl - plan.sl) > tol or not (p.sl > 0 and p.tp > 0):
            self.close_owned(p, "unverified_protection")
            return None, "protection_mismatch"
        if self.exposure(ps, os).risk > self.lim.max_open_risk_usd + 1e-7:
            self.close_owned(p, "aggregate_fill_risk")
            return None, "fill_risk_exceeded"
        tp, _ = target(
            grid, plan.side, plan.lots, p.price_open, prof.target_usd, self.cash(name)
        )
        if abs(tp - p.tp) > tol:
            # A different deal price must not silently turn $10 into a different goal.
            if p.profit >= prof.target_usd:
                self.close_owned(p, "cash_target_after_fill")
                return None, "closed_during_batch"
            req = {
                "action": mt.TRADE_ACTION_SLTP,
                "symbol": name,
                "position": p.ticket,
                "sl": plan.sl,
                "tp": tp,
                "magic": self.h.magic,
            }
            check = mt.order_check(req)
            if check is None or check.retcode != 0:
                return None, "target_alignment_rejected"
            self.account()
            rr = mt.order_send(req)
            verify = mt.positions_get(ticket=p.ticket)
            if (
                rr is None
                or rr.retcode != mt.TRADE_RETCODE_DONE
                or not verify
                or not self.owned(verify[0])
                or abs(verify[0].tp - tp) > tol
                or abs(verify[0].sl - plan.sl) > tol
            ):
                return None, "target_alignment_unverified"
        return p.ticket, "ok"

    def batch(self, name, d):
        key = name
        seen = self.state.setdefault("consumed", {})
        if seen.get(key, 0) >= d.close_s:
            return "already_consumed"
        # One opportunity, including when spread/risk rejects it. No queued catch-up trades.
        seen[key] = d.close_s
        self.persist()
        expected = set()
        n = None
        sent = 0
        try:
            while True:
                left, plan, q, i, recent = self.prepare(
                    name, d, expected, None if n is None else n - sent
                )
                if n is None:
                    n = left
                    self.emit(
                        "batch_preview",
                        symbol=name,
                        tf=d.tf,
                        signal=d.close_s,
                        rate=self.syms[name][1].rate,
                        legs=n,
                        goal_per_leg=self.syms[name][1].target_usd,
                        risk_batch=n * plan.risk_usd,
                        **asdict(plan),
                    )
                    if not self.armed:
                        return "shadow_plan_only"
                req = {
                    "action": self.mt.TRADE_ACTION_DEAL,
                    "symbol": name,
                    "type": self.typ(d.side),
                    "volume": plan.lots,
                    "price": plan.entry,
                    "sl": plan.sl,
                    "tp": plan.tp,
                    "deviation": 0,
                    "magic": self.h.magic,
                    "type_time": self.mt.ORDER_TIME_GTC,
                    "type_filling": _filling_mode(self.mt, i),
                    "comment": f"h{d.tf}_{d.close_s}_{sent + 1}",
                }
                chk = self.mt.order_check(req)
                a = self.account()
                if chk is None or chk.retcode != 0:
                    raise Block("broker_precheck_rejected")
                if (
                    not math.isfinite(chk.margin_free)
                    or 100 * chk.margin_free / a.equity < self.lim.min_free_margin_pct
                ):
                    raise Block("margin_floor")
                # Do not send the inspected plan after a change in exposures/quote, a kill, or a stale signal.
                _, again, q2, _, _ = self.prepare(name, d, expected, n - sent)
                if again != plan or (q.bid, q.ask) != (q2.bid, q2.ask):
                    raise Block("quote_or_plan_changed")
                self.state["manual_hold"] = {
                    "symbol": name,
                    "signal": d.close_s,
                    "leg": sent + 1,
                }
                self.state["sends"] = [*recent, time.time()]
                self.persist()
                self.account()
                q3 = self.mt.symbol_info_tick(name)
                now = time.time()
                if (
                    self.kill.exists()
                    or now > d.close_s + self.lim.max_entry_age_s
                    or not fresh(q3, now, self.lim.max_quote_age_s)
                    or (q3.bid, q3.ask) != (q.bid, q.ask)
                ):
                    self.state.pop("manual_hold")
                    self.persist()
                    raise Block("final_quote_or_safety_changed")
                res = self.mt.order_send(req)
                self.emit(
                    "demo_result",
                    symbol=name,
                    leg=sent + 1,
                    signal=d.close_s,
                    request=req,
                    retcode=getattr(res, "retcode", None),
                    price=getattr(res, "price", None),
                    deal=getattr(res, "deal", None),
                )
                ticket, status = self.finalize(name, plan, res, expected)
                if ticket is None:
                    return status + "_manual_hold"
                expected.add(ticket)
                sent += 1
                self.state.pop("manual_hold")
                self.persist()
                if sent == n:
                    return "demo_batch_sent"
        except Block as exc:
            why = str(exc)
            if why == "manual_reconciliation_required":
                return why
            return (
                "manual_hold:"
                if self.state.get("manual_hold")
                else "protected_partial_no_topup:"
                if sent
                else ""
            ) + why
