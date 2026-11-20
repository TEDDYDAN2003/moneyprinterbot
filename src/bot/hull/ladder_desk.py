"""Per-ticket adapter for the Hull cash ladder. No entry or market-close path here."""

from __future__ import annotations

import math
import time
from dataclasses import asdict

from bot.zones.monitor import fresh

from .config import LADDER, TARGET_POLICY
from .ladder import Protection, executable, inside, percentages, propose, still_valid
from .orders import Block, Grid


def opened_ms(p):
    return int(getattr(p, "time_msc", getattr(p, "time", 0) * 1000))


def pair(record):
    return Protection(record["rung"], record["sl"], record["tp"])


def matches_levels(p, levels, grid):
    tol = grid.tick * 1e-6
    return abs(p.sl - levels.sl) <= tol and abs(p.tp - levels.tp) <= tol


class TicketLadders:
    def __init__(self, desk):
        self.d = desk
        self.sample = {}
        self.noted = {}
        self.cooldown = {}

    @property
    def book(self):
        return self.d.state.get("ladder_by_ticket", {})

    def needs_review(self):
        return any(r.get("pending") or r.get("hold") for r in self.book.values())

    def enroll(self, p, plan, tp):
        # Called only after a *new* fill and its protection have been reconciled.
        key = str(p.ticket)
        self.d.state.setdefault("ladder_by_ticket", {})[key] = {
            "policy": LADDER,
            "symbol": p.symbol,
            "identifier": int(getattr(p, "identifier", p.ticket)),
            "opened_msc": opened_ms(p),
            "side": plan.side,
            "entry": float(p.price_open),
            "lots": float(p.volume),
            "target_policy": TARGET_POLICY,
            "configured_target_usd": plan.configured_target_usd,
            "entry_spread_usd": plan.entry_spread_usd,
            "base_usd": plan.goal_usd,
            "rung": 0,
            "sl": plan.sl,
            "tp": tp,
            "confirmed_tick_msc": 0,
        }
        self.d.persist()
        self.d.emit("profit_ladder_enrolled", ticket=int(p.ticket), **self.book[key])

    def same_position(self, p, key, r):
        return (
            self.d.owned(p)
            and str(p.ticket) == key
            and r["policy"] == LADDER
            and p.symbol == r["symbol"]
            and p.type == self.d.typ(r["side"])
            and int(getattr(p, "identifier", p.ticket)) == r["identifier"]
            and opened_ms(p) == r["opened_msc"]
            and p.price_open == r["entry"]
            and math.isclose(p.volume, r["lots"], rel_tol=0, abs_tol=1e-9)
        )

    def note(self, key, r, reason, **more):
        if self.noted.get(key) != reason:
            self.d.emit(
                "profit_ladder_deferred",
                symbol=r["symbol"],
                ticket=int(key),
                confirmed_rung=r["rung"],
                reason=reason,
                **more,
            )
            self.noted[key] = reason

    def hold(self, key, r, reason):
        if not r.get("hold"):
            r["hold"] = reason
            self.d.persist()
        self.note(key, r, reason)

    def prune(self, active):
        stale = (
            set(self.book) - active
        )  # full account snapshot, not just enabled symbols
        for key in stale:
            del self.book[key]
            self.sample.pop(key, None)
            self.noted.pop(key, None)
            self.cooldown.pop(key, None)
        if stale:
            self.d.persist()

    def confirm(self, p, key, r, grid):
        """Broker read-back, including after a crash/uncertain acknowledgement."""
        intent = r["pending"]
        levels = pair(intent)
        if not self.same_position(p, key, r) or not matches_levels(p, levels, grid):
            return False
        r.update(**asdict(levels), confirmed_tick_msc=intent["tick_time_msc"])
        del r["pending"]
        self.d.persist()
        self.noted.pop(key, None)
        trigger, lock, tp = percentages(levels.rung)
        self.d.emit(
            "profit_ladder_step",
            ticket=int(key),
            symbol=p.symbol,
            base_usd=r["base_usd"],
            entry=r["entry"],
            lots=r["lots"],
            side=r["side"],
            trigger_pct=trigger,
            lock_pct=lock,
            target_pct=tp,
            **intent,
        )
        return True

    def tick(self, q, r):
        if not fresh(q, time.time(), self.d.lim.max_quote_age_s):
            raise Block("ladder_quote_unavailable_or_stale")
        if q.time_msc < max(r["opened_msc"], r["confirmed_tick_msc"]):
            raise Block("ladder_out_of_order_quote")
        return executable(r["side"], q.bid, q.ask)

    def barrier_seen(self, key, r, old, mark, ms):
        if inside(r["side"], mark, old):
            return False
        # An attached exit may already be in flight. Even if price retreats and
        # the position snapshot lags, do not move the order out of its way.
        event = {
            "barrier": "sl" if r["side"] * (mark - old.sl) <= 0 else "tp",
            "mark": mark,
            "tick_time_msc": int(ms),
            **asdict(old),
        }
        r["broker_exit_seen"] = event
        self.d.persist()
        self.d.emit(
            "profit_ladder_broker_barrier", symbol=r["symbol"], ticket=int(key), **event
        )
        return True

    def supervise(self, p, quotes, grids, plans):
        key = str(p.ticket)
        r = self.book[key]
        if not self.d.armed or not self.d.owned(p):
            return
        if r.get("hold"):
            self.note(key, r, r["hold"])
            return
        if not self.same_position(p, key, r):
            self.hold(key, r, "ladder_ticket_changed_review")
            return
        name = p.symbol
        mt = self.d.mt
        try:
            if name not in grids:
                grids[name] = Grid.from_info(mt.symbol_info(name))
            grid = grids[name]
            if r.get("pending"):
                if not self.confirm(p, key, r, grid):
                    self.note(key, r, "ladder_ack_pending_review")
                return  # never resend an uncertain request
            if r.get("broker_exit_seen"):
                return
            old = pair(r)
            if not matches_levels(p, old, grid):
                self.hold(key, r, "ladder_external_levels_review")
                return
            if self.d.state.get("manual_hold"):
                self.note(key, r, "manual_reconciliation_required")
                return
            if name not in quotes:
                quotes[name] = mt.symbol_info_tick(name)
            q = quotes[name]
            mark = self.tick(q, r)
            sample = (q.time_msc, q.bid, q.ask)
            prev = self.sample.get(key)
            if prev == sample or (prev and sample[0] < prev[0]):
                return
            self.sample[key] = sample
            if self.barrier_seen(
                key, r, old, mark, q.time_msc
            ) or time.monotonic() < self.cooldown.get(key, 0):
                return
            args = r["side"], r["lots"], r["entry"], r["base_usd"]
            cache_key = (name, args, old, mark)
            if cache_key not in plans:
                plans[cache_key] = propose(grid, *args, old, mark, self.d.cash(name))
            new = plans[cache_key]
            if new is not None:
                self.modify(p, key, r, grid, old, new, args, q)
        except Block as exc:
            self.note(key, r, str(exc))

    def modify(self, p, key, r, grid, old, new, args, sampled):
        d, mt = self.d, self.d.mt
        if not d.armed or d.kill.exists():
            return
        d.account()  # recheck dedicated account, permissions and identity
        req = {
            "action": mt.TRADE_ACTION_SLTP,
            "symbol": p.symbol,
            "position": int(p.ticket),
            "sl": new.sl,
            "tp": new.tp,
            "magic": d.h.magic,
        }
        checked = mt.order_check(req)
        if checked is None or checked.retcode != 0:
            self.cooldown[key] = time.monotonic() + 1
            self.note(
                key,
                r,
                "ladder_precheck_rejected",
                retcode=getattr(checked, "retcode", None),
            )
            return
        # Durable before the send: if this process dies, confirm both levels from
        # the broker or hold; never infer an amendment from an ack or retry blind.
        r["pending"] = {
            **asdict(new),
            "tick_time_msc": int(sampled.time_msc),
            "mark": executable(r["side"], sampled.bid, sampled.ask),
        }
        d.persist()
        d.account()
        try:
            live = mt.positions_get(ticket=p.ticket)
            if live is None or len(live) != 1:
                raise Block("ladder_ticket_unavailable_before_send")
            if not self.same_position(live[0], key, r) or not matches_levels(
                live[0], old, grid
            ):
                raise Block("ladder_ticket_or_levels_changed_before_send")
            current_grid = Grid.from_info(mt.symbol_info(p.symbol))
            last = mt.symbol_info_tick(p.symbol)
            mark = self.tick(last, r)
            if (
                last.time_msc < sampled.time_msc
                or current_grid != grid
                or self.barrier_seen(key, r, old, mark, last.time_msc)
                or not still_valid(grid, *args, old, new, mark, d.cash(p.symbol))
                or not fresh(last, time.time(), d.lim.max_quote_age_s)
                or d.kill.exists()
            ):
                raise Block("ladder_quote_or_safety_changed")
        except Block:
            del r["pending"]  # definitely no order_send on this path
            d.persist()
            raise
        # One ticket-specific request with BOTH legal levels. An exception leaves
        # the durable pending record intact for read-back on restart.
        result = mt.order_send(req)
        code = getattr(result, "retcode", None)
        d.account()
        observed = mt.positions_get(ticket=p.ticket)
        if observed is not None and not observed:
            # Broker stop, TP or separate exit won the race; no new position/order.
            self.note(key, r, "ladder_ticket_closed_during_update", retcode=code)
            return
        if (
            observed is not None
            and len(observed) == 1
            and self.same_position(observed[0], key, r)
        ):
            if self.confirm(observed[0], key, r, grid):
                return
            refused = {
                getattr(mt, n)
                for n in (
                    "TRADE_RETCODE_REJECT",
                    "TRADE_RETCODE_INVALID_STOPS",
                    "TRADE_RETCODE_FROZEN",
                    "TRADE_RETCODE_PRICE_CHANGED",
                    "TRADE_RETCODE_TOO_MANY_REQUESTS",
                )
                if hasattr(mt, n)
            }
            if code in refused and matches_levels(observed[0], old, grid):
                # Definite refusal AND the original pair is still attached.
                del r["pending"]
                d.persist()
                self.cooldown[key] = time.monotonic() + 1
                self.note(key, r, "ladder_broker_refused", retcode=code)
                return
        self.note(key, r, "ladder_ack_pending_review", retcode=code)
