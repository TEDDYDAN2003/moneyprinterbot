"""Single-writer pending batch book. A placement receipt is NEVER a fill.

Independent clocks: admission & all submissions use the existing short window;
only acknowledged orders can wait for a retest until broker-side expiration.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict

from bot.broker import DemoSafetyError, _side_is_allowed, fresh, snapshot
from bot.config.entry_sessions import EntrySessionVeto

from .config import LADDER, PAIR_MODE, PAIRS, RETEST, STOP_SPREAD_POLICY, TF_NAME
from .notifier import notify_blocked_setup
from .orders import Block, Exposure, Grid, Plan, room
from .retest import GOAL, POLICY, capability, level, placement_check

DONE = {
    "filled",
    "flat",
    "cancelled",
    "expired",
    "rejected",
    "not_sent",
    "partial_flatten",
}

RETEST_MARKET_FALLBACK_REASONS = {
    "retest_quote_drift",
    "retest_already_touched",
    "retest_placement_gap",
    "no_one_tick_pullback_inside_candle_and_atr_cap",
    "no_tradable_retest_tick",
}


def approx(a, b, tol=1e-8):
    return isinstance(a, (int, float)) and math.isfinite(a) and abs(a - b) <= tol


class RetestDesk:
    def __init__(self, desk):
        self.d = desk
        self.mt = desk.mt
        if desk.h.execution_mode != PAIR_MODE:
            raise DemoSafetyError("Retest requires paired_structure")
        self.active = (
            desk.state.setdefault("retest_batches_v1", {}) if desk.armed else {}
        )
        self.busy = False  # supervising inventory
        self.dispatching = None  # only this book must not be settled mid-dispatch

    def signature(self, name):
        d = self.d
        return [
            *d.ident,
            d.h.magic,
            d.indicator_key,
            d.h.exit_policy,
            d.syms[name][1].model_dump(mode="json"),
            d.h.retest_fraction,
            d.h.retest_atr_cap,
        ]

    def recover(self, frames):
        for name, b in self.active.items():
            if (
                name not in self.d.syms
                or b.get("signature") != self.signature(name)
                or b.get("parent") not in frames
                or b.get("child") != PAIRS.get(b.get("parent"))
                or b.get("policy") != POLICY
            ):
                raise DemoSafetyError(
                    "Unresolved retest batch requires its original account, profile, pair and pricing settings"
                )
        for name, b in self.active.items():
            self.invalidate(b, "restart_no_replay", flatten=True)
            for l in b["legs"]:
                if l["status"] == "submitting":
                    self.hold(b, l, "restart_unknown_ack_no_resend")

    def lanes(self):
        return {(sym, b["parent"]) for sym, b in self.active.items()}

    def blocked(self, name):
        if any(b.get("review") for b in self.active.values()):
            return "retest_reconciliation_required"
        return "retest_batch_already_reserved" if name in self.active else ""

    def claims(self, p):
        b = self.active.get(p.symbol)
        return bool(
            b
            and self.d.owned(p)
            and p.type == self.d.typ(b["side"])
            and any(
                l.get("order") == getattr(p, "identifier", None)
                and l["status"] not in DONE
                for l in b["legs"]
            )
        )

    def invalidate(self, b, why, *, flatten=False):
        if not b.get("cancel") or (flatten and not b.get("flatten")):
            b["cancel"] = why
            b["flatten"] = bool(flatten or b.get("flatten"))
            self.d.persist()
            self.d.emit(
                "retest_cancel_intent",
                symbol=b["name"],
                tf=b["parent"],
                signal=b["stamp"],
                reason=why,
            )

    def interrupt(self, name, parent, ms, raw, child=None):
        b = self.active.get(name)
        if (
            b
            and b["parent"] == parent
            and (child is None or b["child"] == child)
            and b["stamp"] * 1000 <= ms
            and raw in (0, -b["side"])
        ):
            self.invalidate(
                b,
                "confirmed_lane_mss"
                if not raw
                else "opposite_pair_trigger_hull"
                if child
                else "opposite_hull_label",
                flatten=True,
            )

    def hold(self, b, l, reason):
        if l.get("review") != reason:
            l["review"] = reason
            b["review"] = True
            self.invalidate(b, "reconciliation_required", flatten=True)
            self.d.persist()
            self.d.emit(
                "retest_review",
                symbol=b["name"],
                signal=b["stamp"],
                leg=l["leg"],
                order=l.get("order"),
                reason=reason,
            )
            notify_blocked_setup(
                symbol=b["name"],
                tf=TF_NAME.get(b.get("parent", 0), b.get("parent", "?")),
                trigger_tf=TF_NAME.get(b.get("child", 0), b.get("child")),
                side=b.get("side", 0),
                reason=reason,
                extra="batch flattened",
            )

    def known(self):
        return {
            l["order"]: (b, l)
            for b in self.active.values()
            for l in b["legs"]
            if l.get("order") and l["status"] not in DONE
        }

    def identity(self, o, b, l, *, historical=False):
        q = l["request"]
        ms = getattr(o, "time_setup_msc", 0)
        magic_ok = o.magic == q["magic"] or (
            historical and o.magic == 0 and getattr(o, "state", 0) == 4
        )
        comment_ok = o.comment == q["comment"] or (
            historical and o.comment == "" and getattr(o, "state", 0) == 4
        )
        return (
            o.ticket == l.get("order")
            and o.symbol == q["symbol"]
            and magic_ok
            and o.type == q["type"]
            and comment_ok
            and b["stamp"] * 1000 <= ms < b["expiry"] * 1000
            and approx(o.volume_initial, q["volume"])
            and approx(o.price_open, q["price"], b["tick"] * 1e-6)
        )

    def intact(self, o, b, l):
        q = l["request"]
        tol = b["tick"] * 1e-6
        return (
            self.identity(o, b, l)
            and o.time_expiration == b["expiry"]
            and o.type_time == q["type_time"]
            and approx(o.sl, q["sl"], tol)
            and approx(o.tp, q["tp"], tol)
        )

    def reserved(self, os, ps=()):
        known = self.known()
        visible = {}
        positions = {getattr(p, "identifier", None): p for p in ps}
        for o in os:
            if o.ticket not in known or not self.intact(o, *known[o.ticket]):
                raise Block("foreign_or_changed_pending_order")
            visible[o.ticket] = o.volume_current
        count = 0
        lots = risk = 0.0
        for ticket, (b, l) in known.items():
            plan = b["plan"]
            p = positions.get(ticket)
            v = visible.get(
                ticket, max(0, plan["lots"] - (p.volume if p and self.claims(p) else 0))
            )
            if not math.isfinite(v) or not 0 <= v <= plan["lots"] + 1e-8:
                raise Block("invalid_pending_volume")
            if v > 0:
                count += 1
                lots += v
                risk += v / plan["lots"] * plan["risk_usd"]
        if not self.d.armed:
            for b in self.active.values():
                n = b["count"]
                count += n
                lots += n * b["plan"]["lots"]
                risk += n * b["plan"]["risk_usd"]
        return Exposure(count, lots, risk)

    def pending_terms(self, account, orders, positions):
        """Reserve accepted-but-unpublished orders too. Never poll for an acknowledgement to appear.

        MT5 margin_free may ALREADY exclude margin_initial held for pending orders.
        Credit that *reported* amount only for visible, verified orders on this dedicated
        account, up to their reserved activation margin. Don't infer credit from a balance delta.
        Called after exposure() has verified all orders and rejected foreign exposure.
        """
        if not self.d.armed:
            return [
                (b, b["count"], b["count"] * b["plan"]["lots"])
                for b in self.active.values()
            ], sum(b["margin"] * b["count"] for b in self.active.values())
        visible = {o.ticket: o.volume_current for o in orders}
        activated = {
            getattr(pos, "identifier", None): pos.volume
            for pos in positions
            if self.claims(pos)
        }
        rows = []
        gross = visible_margin = 0.0
        for ticket, (b, leg) in self.known().items():
            qty = leg["request"]["volume"]
            v = visible.get(ticket, max(0, qty - activated.get(ticket, 0)))
            if v <= 0:
                continue
            rows.append((b, 1, v))
            charge = b["margin"] * v / qty
            gross += charge
            if ticket in visible:
                visible_margin += charge
        held = getattr(account, "margin_initial", 0.0)
        if not isinstance(held, (int, float)) or not math.isfinite(held) or held < 0:
            held = 0
        return rows, max(0, gross - min(visible_margin, held))

    @staticmethod
    def broker_room(rows, name, side, plan, n, account, info):
        cap = getattr(account, "limit_orders", 0)
        if cap and sum(count for _, count, _ in rows) + n > cap:
            return "broker_pending_order_limit"
        max_lots = getattr(info, "volume_limit", 0)
        if (
            max_lots
            and sum(v for b, _, v in rows if b["name"] == name and b["side"] == side)
            + plan.lots * n
            > max_lots + 1e-8
        ):
            return "symbol_pending_volume_limit"
        return ""

    def expires(self, name, r):
        d = self.d
        gate = d.session_gate(name)
        extra = d.syms[name][1].session == "configured"
        end = r.close_s + r.candle_tf * 60
        # At most M10 = 600 seconds; evaluated ONCE, never a sleep in the send path.
        for t in range(r.close_s, end):
            if gate.veto(t) or (
                extra and (d.hours.status(t)[0] or d.hours.status(t + 0.999999)[0])
            ):
                return t
        return end

    def stage(self, name, w, plan, n, q, info):
        d = self.d
        mt = self.mt
        r = w.reading
        g = Grid.from_info(info)
        if reason := self.blocked(name):
            raise Block(reason)
        if reason := capability(mt, info):
            raise Block(reason)
        exp = self.expires(name, r)
        if exp <= d.now():
            raise Block("retest_no_open_session_lifetime")
        margin = mt.order_calc_margin(d.typ(r.side), name, plan.lots, plan.entry)
        if (
            not isinstance(margin, (int, float))
            or not math.isfinite(margin)
            or margin < 0
        ):
            raise Block("no_margin_estimate")
        self.d.session_gate(name).require("retest_plan", decision=d.now())
        ref, segment = level(
            r.cross_bar, r.side, r.atr, g.tick, d.h.retest_fraction, d.h.retest_atr_cap
        )
        if ref != plan.entry:
            raise Block("retest_plan_changed")
        b = {
            "policy": POLICY,
            "name": name,
            "signature": self.signature(name),
            "parent": r.tf,
            "child": r.candle_tf,
            "side": r.side,
            "stamp": r.close_s,
            "expiry": exp,
            "tick": g.tick,
            "grid": asdict(g),
            "ages": [d.lim.max_entry_age_s, d.lim.max_quote_age_s],
            "count": n,
            "margin": margin,
            "plan": asdict(plan),
            "quote_ms": q.time_msc,
            "bid": q.bid,
            "ask": q.ask,
            "bar": asdict(r.cross_bar),
            "segment": segment,
            "atr": r.atr,
            "fraction": d.h.retest_fraction,
            "atr_cap": d.h.retest_atr_cap,
            "audit": r.entry_audit(),
            "legs": [],
        }
        if not d.armed:
            self.capacity(b, r, plan, 1)
            current, _ = d._entry_quote(name, w, "shadow_limit_decision")
            self.guard(b, w, g, plan, current)
            if current.time_msc < q.time_msc or (
                not d.h.retest_market_fallback
                and max(abs(current.bid - q.bid), abs(current.ask - q.ask))
                > g.tick * (d.h.retest_quote_drift_ticks + 1e-9)
            ):
                raise Block("retest_quote_drift")
            b["shadow_after_ms"] = max(current.time_msc, int(d.now() * 1000))
        self.active[name] = b
        d.open_lanes.add((name, r.tf))
        d.persist()
        d.emit(
            "retest_batch_plan",
            symbol=name,
            tf=r.tf,
            trigger_tf=r.candle_tf,
            signal=r.close_s,
            legs=n,
            decision_ms=round(d.now() * 1000),
            profile_rate=d.syms[name][1].rate,
            configured_target_usd=plan.configured_target_usd,
            initial_goal_usd=plan.goal_usd,
            lots_per_leg=plan.lots,
            risk_per_leg_usd=plan.risk_usd,
            atr=r.atr,
            trade_tick=g.tick,
            min_gap=max(g.tick, g.gap),
            fraction=d.h.retest_fraction,
            atr_cap=d.h.retest_atr_cap,
            side=r.side,
            send_bid=q.bid,
            send_ask=q.ask,
            entry_route=RETEST,
            entry_policy=POLICY,
            target_policy=GOAL,
            limit=ref,
            **plan.stop_audit(q.bid, q.ask),
            sl=plan.sl,
            tp=plan.tp,
            spread_usd=plan.entry_spread_usd,
            quote_msc=q.time_msc,
            expiry=exp,
            segment=segment,
            trigger_bar=b["bar"],
            shadow=not d.armed,
        )
        if not d.armed:
            return "shadow_retest_working"
        self.dispatching = name
        started = time.monotonic()
        try:
            for k in range(1, n + 1):
                self.place_leg(b, w, plan, g, k)
                if d.watch_other_symbols:
                    d.watch_other_symbols(name)
                d.service_if_due()
            outcome = "retest_batch_placed"
        except (Block, EntrySessionVeto) as e:
            outcome = str(e)
            ack_so_far = sum(bool(l.get("order")) for l in b["legs"])
            if (
                d.h.retest_market_fallback
                and outcome in RETEST_MARKET_FALLBACK_REASONS
            ):
                if ack_so_far == 0:
                    self.active.pop(name, None)
                    d.open_lanes.discard((name, r.tf))
                    d.persist()
                else:
                    ps, os = snapshot(mt)
                    if not any(p.symbol == name for p in ps):
                        orders = {o.ticket: o for o in os}
                        for l in b["legs"]:
                            o = orders.get(l.get("order"))
                            if o is not None:
                                self.remove(b, l, o)
                        self.active.pop(name, None)
                        d.open_lanes.discard((name, r.tf))
                        d.persist()
                    elif outcome not in {
                        "entry_window_expired",
                        "retest_activated_during_dispatch",
                        "retest_already_touched",
                        "retest_placement_gap",
                    }:
                        self.invalidate(b, outcome)
            # A pure clock cutoff or the intended pullback stops *new* submissions;
            # it doesn't void legal siblings already resting at the same price.
            elif outcome not in {
                "entry_window_expired",
                "retest_activated_during_dispatch",
                "retest_already_touched",
                "retest_placement_gap",
            }:
                self.invalidate(b, outcome)
        finally:
            self.dispatching = None
            ack = sum(bool(l.get("order")) for l in b["legs"])
            d.emit(
                "retest_dispatch",
                symbol=name,
                tf=r.tf,
                signal=r.close_s,
                planned=n,
                acknowledged=ack,
                dispatch_ms=round(1000 * (time.monotonic() - started), 3),
                close_to_last_response_ms=round(1000 * (d.now() - r.close_s), 3),
                outcome=locals().get("outcome", "interrupted"),
            )
        return outcome

    def guard(self, b, w, g, plan, q):
        d = self.d
        mt = self.mt
        name = b["name"]
        r = w.reading
        p = d.syms[name][1]
        if (
            d.kill.exists()
            or b.get("cancel")
            or d.state.get("manual_hold")
            or d.exits
            or d.ladders.needs_review()
            or any(x.get("review") for x in self.active.values())
        ):
            raise Block("retest_safety_changed")
        at = d._quote_clock(d.session_gate(name), w, q, "limit_submit").checked
        d.session_gate(name).require(
            "limit_submit", signal=r.close_s, quote=q.time_msc / 1000, decision=at
        )
        if (
            at >= b["expiry"]
            or (p.session == "configured" and d.hours.status(at)[0])
            or d.mss_book.get(name, {}).get(str(r.tf), -1) >= r.close_s
            or d.pair_fence(name, r.tf, r.candle_tf, -r.side) >= r.signal_msc
        ):
            raise Block("retest_invalidated_or_expired")
        info = mt.symbol_info(name)
        if (
            capability(mt, info)
            or Grid.from_info(info) != g
            or getattr(info, "chart_mode", None)
            != getattr(mt, "SYMBOL_CHART_MODE_BID", 0)
            or not _side_is_allowed(mt, info, "BUY" if r.side == 1 else "SELL")
        ):
            raise Block("retest_symbol_spec_changed")
        # Siblings share one frozen placement bracket. Reject an under-buffered
        # SELL, even within one tick of drift. This is NOT a market-fallback reason.
        if not plan.covers_spread(q.bid, q.ask):
            raise Block("retest_stop_spread_buffer_insufficient")
        placement_check(
            r.side, plan.entry, plan.sl, plan.tp, g, q.bid, q.ask, p.max_spread
        )

    def capacity(self, b, r, plan, k):
        d = self.d
        mt = self.mt
        name = b["name"]
        a = d.account()
        ps, os = snapshot(mt)
        e = d.exposure(ps, os)
        remain = b["count"] - k + 1
        here = [p for p in ps if p.symbol == name]
        if here:
            if all(self.claims(p) for p in here):
                raise Block("retest_activated_during_dispatch")
            raise Block("retest_book_moved_during_dispatch")
        rows, unfunded = self.pending_terms(a, os, ps)
        if why := self.broker_room(
            rows, name, r.side, plan, remain, a, mt.symbol_info(name)
        ):
            raise Block(why)
        if why := room(d.lim, e, plan, remain):
            raise Block(why)
        margin = mt.order_calc_margin(d.typ(r.side), name, plan.lots, plan.entry)
        if (
            not isinstance(margin, (int, float))
            or not math.isfinite(margin)
            or margin < 0
        ):
            raise Block("no_margin_estimate")
        free = getattr(a, "margin_free", None)
        projected = (
            free - unfunded - remain * margin
            if isinstance(free, (int, float))
            else None
        )
        if why := d._margin_check(name, r, a, "retest_batch", remain, projected):
            raise Block(why)
        return a

    def place_leg(self, b, w, plan, g, k):
        d = self.d
        mt = self.mt
        name = b["name"]
        r = w.reading
        leg_start = time.monotonic()
        # Refresh mutable safety, not the candles, entry or target solver for every sibling.
        a = d.account()
        q, _ = d._entry_quote(name, w, "limit_precheck")
        self.guard(b, w, g, plan, q)
        req = {
            "action": mt.TRADE_ACTION_PENDING,
            "symbol": name,
            "type": mt.ORDER_TYPE_BUY_LIMIT
            if r.side == 1
            else mt.ORDER_TYPE_SELL_LIMIT,
            "price": plan.entry,
            "volume": plan.lots,
            "sl": plan.sl,
            "tp": plan.tp,
            "magic": d.h.magic,
            "type_time": mt.ORDER_TIME_SPECIFIED,
            "expiration": b["expiry"],
            "type_filling": mt.ORDER_FILLING_RETURN,
            "comment": f"r{r.tf}x{r.candle_tf}_{r.close_s}_{k}",
        }
        check = mt.order_check(req)
        if getattr(check, "retcode", None) != 0:
            raise Block("retest_order_check_rejected")
        if why := d._margin_check(
            name, r, a, "retest_order_check", 1, getattr(check, "margin_free", None)
        ):
            raise Block(why)
        # A durable, unique intent per leg. An unknown reply is NOT permission to retry.
        l = {"leg": k, "status": "submitting", "request": req}
        b["legs"].append(l)
        d.persist()
        try:
            self.capacity(b, r, plan, k)
            q2 = mt.symbol_info_tick(name)
            if q2 is None or (q2.time_msc, q2.bid, q2.ask) != (
                q.time_msc,
                q.bid,
                q.ask,
            ):
                q2, _ = d._entry_quote(name, w, "limit_pre_send")
            self.guard(b, w, g, plan, q2)
            if (
                q2.time_msc < q.time_msc
                or (
                    not d.h.retest_market_fallback
                    and max(abs(q2.bid - q.bid), abs(q2.ask - q.ask))
                    > g.tick * (d.h.retest_quote_drift_ticks + 1e-9)
                )
            ):
                raise Block("retest_quote_drift")
            now = d._quote_clock(d.session_gate(name), w, q2, "retest_send").checked
            # Last local decision after potentially slow MT5 reads. No filesystem/RPC
            # before order_send after these last clock, session, kill and fence checks.
            d.session_gate(name).require("retest_send", decision=now)
            if (
                d.kill.exists()
                or b.get("cancel")
                or (d.syms[name][1].session == "configured" and d.hours.status(now)[0])
            ):
                raise Block("retest_safety_changed")
            if now >= b["expiry"]:
                raise Block("retest_expired")
        except (Block, EntrySessionVeto, DemoSafetyError):
            l["status"] = "not_sent"
            d.persist()
            raise
        start = time.monotonic()
        try:
            res = mt.order_send(dict(req))
        except Exception as exc:
            self.hold(b, l, "placement_exception_unknown_ack")
            raise Block("placement_exception_unknown_ack") from exc
        elapsed = 1000 * (time.monotonic() - start)
        ret = getattr(res, "retcode", None)
        order = getattr(res, "order", 0)
        l.update(
            submit_ms=int(now * 1000),
            send_ms=q2.time_msc,
            send_bid=q2.bid,
            send_ask=q2.ask,
            retcode=ret,
        )
        if (
            ret in (10008, 10009, 10010)
            and isinstance(order, int)
            and order > 0
            and order not in self.known()
        ):
            l.update(order=order, status="working")
        elif (
            ret
            in (
                10004,
                10006,
                10013,
                10014,
                10015,
                10016,
                10017,
                10018,
                10019,
                10020,
                10021,
                10022,
                10024,
                10026,
                10027,
                10030,
                10033,
                10034,
                10035,
            )
            and (not order or not mt.orders_get(ticket=order))
        ):
            l["status"] = "rejected"
        else:
            self.hold(b, l, "unknown_placement_ack_no_resend")
        d.persist()
        d.emit(
            "retest_placement_result",
            symbol=name,
            signal=r.close_s,
            tf=r.tf,
            trigger_tf=r.candle_tf,
            leg=k,
            order=order,
            retcode=ret,
            submit_ms=l["submit_ms"],
            close_to_submit_ms=round(1000 * (now - r.close_s), 3),
            order_send_roundtrip_ms=round(elapsed, 3),
            placement_ms=round(1000 * (time.monotonic() - leg_start), 3),
            ack_ms=round(d.now() * 1000),
            status=l["status"],
        )
        if l["status"] != "working":
            if l["status"] == "rejected" and ret == 10015 and d.h.retest_market_fallback:
                raise Block("retest_already_touched")
            raise Block(l.get("review", "retest_placement_rejected"))

    def remove(self, b, l, o):
        d = self.d
        mt = self.mt
        if not d.armed or l.get("removal") or not self.identity(o, b, l):
            return
        d.account()
        l["removal"] = "submitted"
        d.persist()
        d.account()
        try:
            res = mt.order_send(
                {
                    "action": mt.TRADE_ACTION_REMOVE,
                    "order": o.ticket,
                    "symbol": o.symbol,
                    "magic": d.h.magic,
                }
            )
        except Exception:
            self.hold(b, l, "cancel_exception_unknown_ack")
            return
        code = getattr(res, "retcode", None)
        l["removal_code"] = code
        d.persist()
        d.emit(
            "retest_cancel_result",
            symbol=b["name"],
            order=o.ticket,
            leg=l["leg"],
            reason=b.get("cancel"),
            retcode=code,
        )
        # Never infer cancelled from an acknowledgement; expiry is broker-side.
        if code != 10009:
            self.hold(b, l, "cancel_race_or_unknown_ack")

    def shutdown(self):
        if self.active:
            for b in self.active.values():
                self.invalidate(b, "scanner_shutdown", flatten=True)
            # One removal pass and one fresh reconciliation; never loop waiting for MT5.
            if self.d.armed:
                self.d.supervise()
                if self.active:
                    self.d.supervise()
            else:
                self.service()

    def service(self):
        if self.busy or not any(name != self.dispatching for name in self.active):
            return
        d = self.d
        mt = self.mt
        a = d.account()
        self.busy = True
        try:
            ps, os = snapshot(mt)
            portfolio_veto = ""
            try:
                e = d.exposure(ps, os)
                _, unfunded = self.pending_terms(a, os, ps)
                free = getattr(a, "margin_free", None)
                if (
                    not isinstance(free, (int, float))
                    or not math.isfinite(free)
                    or (
                        free - unfunded
                        < a.equity * d.lim.min_free_margin_pct / 100 - 1e-8
                    )
                ):
                    portfolio_veto = "working_funding_changed"
                if d.state.get("manual_hold") or d.ladders.needs_review():
                    portfolio_veto = "working_account_review"
                if any(x.get("review") for x in self.active.values()):
                    portfolio_veto = "working_reconciliation_required"
            except Block as exc:
                portfolio_veto = str(exc)

            orders = {o.ticket: o for o in os}
            for name, b in list(self.active.items()):
                if name == self.dispatching:
                    continue
                now = d.now()
                info = mt.symbol_info(name)
                q = mt.symbol_info_tick(name)
                profile = d.syms[name][1]
                try:
                    valid_spec = (
                        not capability(mt, info)
                        and Grid.from_info(info) == Grid(**b["grid"])
                        and getattr(info, "chart_mode", None)
                        == getattr(mt, "SYMBOL_CHART_MODE_BID", 0)
                        and _side_is_allowed(
                            mt, info, "BUY" if b["side"] == 1 else "SELL"
                        )
                    )
                except Block:
                    valid_spec = False
                if d.kill.exists():
                    self.invalidate(b, "kill", flatten=True)
                elif now >= b["expiry"]:
                    self.invalidate(b, "expiry")
                elif d.session_gate(name).veto(now) or (
                    d.syms[name][1].session == "configured" and d.hours.status(now)[0]
                ):
                    self.invalidate(b, "entry_session_closed", flatten=True)
                elif portfolio_veto or (
                    why := room(d.lim, e, Plan.from_saved(b["plan"]), 0)
                ):
                    self.invalidate(b, portfolio_veto or why, flatten=True)
                elif (
                    not valid_spec
                    or not fresh(q, now, d.lim.max_quote_age_s)
                    or (
                        profile.max_spread is not None
                        and q.ask - q.bid > profile.max_spread + b["tick"] * 1e-7
                    )
                ):
                    self.invalidate(b, "working_quote_or_spec_unusable", flatten=True)
                elif any(
                    p.symbol == name
                    and not (
                        self.claims(p)
                        or (
                            d.owners.lane(p) == b["parent"]
                            and (d.owners.origin(p) or {}).get("trigger_tf")
                            == b["child"]
                        )
                    )
                    for p in ps
                ):
                    self.invalidate(b, "working_symbol_inventory_changed", flatten=True)
                if not d.armed:
                    p = Plan.from_saved(b["plan"])
                    hit = (
                        not b.get("cancel")
                        and fresh(q, now, d.lim.max_quote_age_s)
                        and q.time_msc > b["shadow_after_ms"]
                        and p.side * ((q.ask if p.side == 1 else q.bid) - p.entry) <= 0
                    )
                    if hit or b.get("cancel"):
                        d.emit(
                            "retest_shadow_result",
                            symbol=name,
                            tf=b["parent"],
                            signal=b["stamp"],
                            limit=p.entry,
                            planned=b["count"],
                            outcome="observed_quote_touch_not_fill"
                            if hit
                            else b["cancel"],
                            observed_tick_ms=q.time_msc if hit else None,
                        )
                        self.active.pop(name)
                        d.open_lanes.discard((name, b["parent"]))
                    continue
                for l in b["legs"]:
                    if l["status"] in DONE or not l.get("order"):
                        continue
                    o = orders.get(l["order"])
                    if o is not None:
                        if not self.identity(o, b, l):
                            self.hold(b, l, "order_identity_mismatch")
                            continue
                        if not self.intact(o, b, l):
                            self.invalidate(
                                b, "pending_protection_or_expiry_changed", flatten=True
                            )
                        partial = not approx(o.volume_current, o.volume_initial)
                        if partial:
                            self.invalidate(b, "partial_pending_volume", flatten=True)
                            # The remainder can be frozen or removal can race another fill.
                            # Protect the *proven position* without waiting on the cancellation.
                            self.settle(b, l, ps, os, working=o)
                        if b.get("cancel"):
                            self.remove(b, l, o)
                    else:
                        self.settle(b, l, ps, os)
                if all(l["status"] in DONE for l in b["legs"]):
                    d.emit(
                        "retest_batch_settled",
                        symbol=name,
                        tf=b["parent"],
                        signal=b["stamp"],
                        planned=b["count"],
                        outcomes=[l["status"] for l in b["legs"]],
                        fill_deals=sorted(
                            {did for leg in b["legs"] for did in leg.get("deals", [])}
                        ),
                        side=b["side"],
                        trigger_tf=b["child"],
                    )
                    self.active.pop(name)
                    d.persist()
            d.open_lanes.update(self.lanes())
        finally:
            self.busy = False

    def await_history(self, b, l):
        first = l.setdefault("awaiting_since", self.d.now())
        if self.d.now() - first >= 10:
            self.hold(b, l, "awaiting_order_deal_position_proof")

    def settle(self, b, l, ps, os, *, working=None):
        d = self.d
        mt = self.mt
        tid = l["order"]
        if working is None:
            tab = mt.history_orders_get(ticket=tid)
            if not tab:
                return self.await_history(b, l)
            if len(tab) != 1 or not self.identity(tab[0], b, l, historical=True):
                self.hold(b, l, "historical_order_identity_mismatch")
                return
            o = tab[0]
            if o.state != 4 and not self.intact(o, b, l):
                self.invalidate(
                    b, "historical_brackets_or_expiry_changed", flatten=True
                )
            if o.state not in (2, 4, 5, 6):
                return self.await_history(b, l)  # CANCELED, FILLED, REJECTED, EXPIRED
        else:
            o = working
        state = o.state
        pid = getattr(o, "position_id", 0) or tid
        name = b["name"]
        side = b["side"]
        plan = Plan.from_saved(b["plan"])
        tol = b["tick"] * 1e-6
        # MT5 python API history_deals_get filters by position_id (which equals order ticket tid on fill)
        ds = mt.history_deals_get(position=pid)
        if ds is None:
            ds = mt.history_deals_get(ticket=tid)
        if ds is None:
            return self.await_history(b, l)
        opening_ds = [x for x in ds if getattr(x, "order", 0) == tid and x.entry == mt.DEAL_ENTRY_IN]
        if not opening_ds and ds and any(getattr(x, "order", 0) == tid for x in ds):
            opening_ds = [x for x in ds if getattr(x, "order", 0) == tid]
        if not opening_ds:
            if (
                working is None
                and state != 4
                and not getattr(o, "position_id", 0)
                and approx(o.volume_initial, o.volume_current)
            ):
                l["status"] = {2: "cancelled", 5: "rejected", 6: "expired"}[state]
                d.persist()
                return
            return self.await_history(b, l)
        ds = opening_ds
        if working is not None and not pid and ds:
            pid = ds[0].position_id
        if (
            not pid
            or len({x.ticket for x in ds}) != len(ds)
            or any(
                x.order != tid
                or x.position_id != pid
                or x.symbol != name
                or x.magic != d.h.magic
                or x.entry != mt.DEAL_ENTRY_IN
                or x.type != d.typ(side)
                or not math.isfinite(x.volume)
                or x.volume <= 0
                or not math.isfinite(x.price)
                or x.price <= 0
                or side * (x.price - plan.entry) > tol
                or not isinstance(x.ticket, int)
                or x.ticket <= 0
                or not isinstance(x.time_msc, int)
                or x.time_msc < max(l["submit_ms"], l["send_ms"], o.time_setup_msc)
                for x in ds
            )
        ):
            self.hold(b, l, "unverified_opening_deal")
            return
        volume = sum(x.volume for x in ds)
        avg = sum(x.price * x.volume for x in ds) / volume
        if volume > plan.lots + 1e-8:
            self.hold(b, l, "opening_volume_exceeds_reservation")
            return
        if not approx(volume, plan.lots - o.volume_current):
            return self.await_history(b, l)  # unfinished deal-history sync
        pos = [p for p in ps if getattr(p, "identifier", 0) == pid]
        if not pos:
            history = mt.history_deals_get(position=pid)
            if history is None:
                return self.await_history(b, l)
            seen = {
                x.ticket
                for x in history
                if x.entry == mt.DEAL_ENTRY_IN and x.position_id == pid
            }
            if seen != {x.ticket for x in ds}:
                return self.await_history(b, l)
            outs = sum(
                x.volume
                for x in history
                if x.position_id == pid
                and x.symbol == name
                and x.entry in (mt.DEAL_ENTRY_OUT, mt.DEAL_ENTRY_OUT_BY)
                and x.type == d.typ(-side)
                and x.time_msc >= min(y.time_msc for y in ds)
            )
            if not approx(outs, volume):
                return self.await_history(b, l)  # no snapshot alone proves flat
            status = "flat"
        else:
            p = pos[0]
            if (
                len(pos) != 1
                or not d.owned(p)
                or p.symbol != name
                or p.type != d.typ(side)
                or not approx(p.price_open, avg, max(tol, b["tick"] / 2 + tol))
                or not math.isfinite(p.volume)
                or not 0 < p.volume <= volume + 1e-8
                or getattr(p, "time_msc", 0) != min(x.time_msc for x in ds)
            ):
                self.hold(b, l, "pending_position_identity_mismatch")
                return
            if not d.owners.bind(
                p, b["parent"], pair_child=b["child"], policy_key=d.indicator_key
            ):
                self.hold(b, l, "owner_identity_changed")
                return
            d.persist()
            key = str(p.ticket)
            origin = d.owners.origin(p)
            if not origin:
                self.hold(b, l, "unverified_position")
                return
            fence = (
                d.pair_fence(name, b["parent"], b["child"], -side) >= b["stamp"] * 1000
                or d.mss_book.get(name, {}).get(str(b["parent"]), -1) >= b["stamp"]
            )
            late = any(x.time_msc >= b["expiry"] * 1000 for x in ds)
            partial = not approx(p.volume, plan.lots)
            changed = (
                not (approx(p.sl, plan.sl, tol) and approx(p.tp, plan.tp, tol))
                and key not in d.ladders.book
                and not l.get("enrolled")
            )
            if changed:
                self.invalidate(b, "position_brackets_changed", flatten=True)
            if b.get("flatten") or fence or late or partial or d.kill.exists():
                why = b.get("cancel") or (
                    "partial_retest_fill"
                    if partial
                    else "retest_fill_after_invalidation"
                )
                d._reserve_exit(
                    p, b["parent"], why, max(x.time_msc for x in ds), child=b["child"]
                )
                d.persist()
                status = "partial_flatten" if partial else "filled"
            elif (d.h.exit_policy == LADDER and key in d.ladders.book) or (
                d.h.exit_policy != LADDER
                and key in d.state.get("target_by_ticket", {})
                and l.get("enrolled")
            ):
                status = "filled"  # idempotent recovery: never reset an existing rung
            else:
                try:
                    ticket, why = d.enroll_fill(
                        name,
                        b["parent"],
                        plan,
                        p,
                        ps,
                        os,
                        pair_child=b["child"],
                        target_policy=GOAL,
                    )
                except Block as exc:
                    ticket, why = None, str(exc)
                if not ticket:
                    self.hold(b, l, why)
                    d._reserve_exit(
                        p,
                        b["parent"],
                        "retest_fill_protection_review",
                        max(x.time_msc for x in ds),
                        child=b["child"],
                    )
                    d.persist()
                    return
                l["enrolled"] = True
                status = "filled"
        if working is not None:
            return  # keep the unfilled remainder reserved
        for x in ds:
            d.emit(
                "retest_fill_receipt",
                symbol=name,
                tf=b["parent"],
                trigger_tf=b["child"],
                signal=b["stamp"],
                entry_route=RETEST,
                pricing_policy=POLICY,
                target_policy=GOAL,
                # Recovery of a legacy placement must not manufacture v2 evidence.
                **(
                    plan.stop_audit(b["bid"], b["ask"])
                    if plan.stop_spread_policy == STOP_SPREAD_POLICY else {}
                ),
                leg=l["leg"],
                order=tid,
                deal=x.ticket,
                position_id=pid,
                fill_ms=x.time_msc,
                price=x.price,
                fill_lots=x.volume,
                request=l["request"],
                submit_ms=l["submit_ms"],
                send_ms=l["send_ms"],
                send_bid=l["send_bid"],
                send_ask=l["send_ask"],
                expiry=b["expiry"],
                trigger_bar=b["bar"],
                segment=b["segment"],
                atr=b["atr"],
                tick=b["tick"],
                fraction=b["fraction"],
                atr_cap=b["atr_cap"],
                max_entry_age_s=b["ages"][0],
                max_quote_age_s=b["ages"][1],
                audit=b["audit"],
            )
        l.update(status=status, deals=[x.ticket for x in ds])
        d.persist()
