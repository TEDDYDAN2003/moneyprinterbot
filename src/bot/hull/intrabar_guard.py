"""Ticket-scoped invalidation of a live entry, including its actual closing print.

The two curves on the entry candle have a fixed previous relationship. The
original cross still exists iff B is strictly on its entry side of that SAME
source A. A label on the following candle is not evidence about this one.
"""

import math

from bot.broker import fresh

from .bar import cross_location
from .config import CROSS_REGION_POLICY, INTRA_ENTRY, REGION_BASIS
from .ladder_desk import matches_levels, pair
from .orders import Block, Grid, spread_adjusted_stop


def valid_origin(g, child, side, opened):
    if (
        not isinstance(g, dict)
        or g.get("policy") != INTRA_ENTRY
        or g.get("side") != side
        or g.get("source")
        not in ("low", "high", "open", "close", "hl2", "hlc3", "ohlc4")
        or type(g.get("ms")) is not int
        or type(g.get("end")) is not int
        or not child
    ):
        return False
    cross = g.get("hull_cross_price")
    if g.get("cross_region_policy") == CROSS_REGION_POLICY and (
        type(cross) not in (int, float) or not math.isfinite(cross) or cross <= 0
    ):
        return False
    if any(
        k in g
        for k in (
            "cross_region_basis",
            "cross_region_location",
            "cross_signal_low",
            "cross_signal_high",
        )
    ):
        loc = cross_location(
            side,
            cross,
            g.get("trigger_bid"),
            g.get("cross_signal_low"),
            g.get("cross_signal_high"),
        )
        if (
            g.get("cross_region_policy") != CROSS_REGION_POLICY
            or g.get("cross_region_basis") != REGION_BASIS
            or g.get("trigger_tf") != child
            or loc not in ("inside", "outside")
            or g.get("cross_region_location") != loc
        ):
            return False
    ms, end = g["ms"], g["end"] * 1000
    if not 0 < end - child * 60000 <= ms < end or ms > opened:
        return False
    status, observed = g.get("status"), g.get("observed_msc")
    return (
        (status == "watch" and observed is None)
        or (status == "confirmed" and observed == end)
        or (status == "invalid" and type(observed) is int and ms <= observed <= end)
    )


def relation(r, source, side):
    a = (
        r.a_low
        if source == "low"
        else r.a_high
        if source == "high"
        else r.a
        if source == r.active_source
        else None
    )
    if a is None or r.b is None or not math.isfinite(a) or not math.isfinite(r.b):
        return None
    return side * (r.b - a) > 0


class OriginGuards:
    def __init__(self, desk):
        self.d = desk

    def watching(self, name, parent, child):
        return [
            (rec, g)
            for rec in self.d.owners.book.values()
            if (g := rec.get("intrabar_origin"))
            and g["status"] == "watch"
            and rec.get("missing_since") is None
            and rec["symbol"] == name
            and rec["magic"] == self.d.h.magic
            and rec["tf"] == parent
            and rec.get("trigger_tf") == child
        ]

    def has_pending_cross_stops(self):
        return any(
            (g := rec.get("intrabar_origin"))
            and g.get("status") == "confirmed"
            and g.get("cross_region_policy") == CROSS_REGION_POLICY
            and g.get("cross_region_status") in ("pending", "unknown_ack")
            and rec.get("missing_since") is None
            for rec in self.d.owners.book.values()
        )

    def unreconciled(self):
        return any(
            rec.get("missing_since") is None
            and (rec.get("intrabar_origin") or {}).get("cross_region_status")
            == "unknown_ack"
            for rec in self.d.owners.book.values()
        )

    def observe(self, name, parent, r):
        changed = []
        stamp = r.signal_msc
        for rec, g in self.watching(name, parent, r.tf):
            if g["end"] != r.close_s or stamp < g["ms"]:
                continue
            holds = relation(r, g["source"], g["side"])
            cross = g.get("hull_cross_price")
            final = r.trigger is None
            # Only the original closed Bid candle can supply this price. Keep
            # pre-intersection tickets on their recorded, relation-only rule.
            wrong_close = (
                final and cross is not None and g["side"] * (r.close - cross) <= 0
            )
            if not wrong_close and (holds is None or (holds and not final)):
                continue
            confirmed = holds is True and not wrong_close
            g.update(status="confirmed" if confirmed else "invalid", observed_msc=stamp)
            if final and g.get("cross_region_policy") == CROSS_REGION_POLICY:
                g["cross_region_candle_close"] = r.close
                if not confirmed:
                    g["cross_region_status"] = "exit_pending"
                elif g.get("cross_region_status") == "awaiting_origin_close":
                    # Includes defer-all guards already enrolled by the previous release.
                    # An outside level was applied on entry: never re-price its spread.
                    g["cross_region_status"] = "pending"
            changed.append((rec, g))
        if not changed:
            return 0
        d = self.d
        d.persist()  # latch before inventory/ack; no recross cancels this exit intent
        d.emit(
            "origin_cross_check",
            symbol=name,
            parent_tf=parent,
            trigger_tf=r.tf,
            bar_end_s=r.close_s,
            observed_msc=stamp,
            phase="live" if r.trigger else "final_close",
            candle_close=r.close if r.trigger is None else None,
            tickets={str(rec["ticket"]): g["status"] for rec, g in changed},
        )
        if not any(g["status"] == "invalid" for _, g in changed):
            self.activate_pending()
            return 0
        matched = []
        for p in (d.mt.positions_get() or ()) if d.armed else ():
            if p.symbol == name and self.reserve(p):
                matched.append(p)
        if matched:
            d.persist()
        for p in matched:
            origin = d.owners.origin(p)["intrabar_origin"]
            if (
                origin.get("cross_region_status") == "exit_pending"
                and origin.get("observed_msc") == stamp
            ):
                self._activation_event(
                    p,
                    origin,
                    "exit_pending",
                    None,
                    None,
                    reason="origin_close_invalidated",
                )
            d._service_exit(p, d.exits[str(p.ticket)])
        return len(matched)

    def activate_pending(self, positions=None):
        """After a valid origin close, tighten the broker SL to the Hull intersection."""
        d = self.d
        if not d.armed:
            return 0
        if positions is None:
            positions = d.mt.positions_get()
        if positions is None:
            return 0
        changed = 0
        for p in positions:
            if not d.owned(p):
                continue
            rec = d.owners.origin(p)
            g = (rec or {}).get("intrabar_origin")
            if not (
                g
                and g.get("status") == "confirmed"
                and g.get("cross_region_policy") == CROSS_REGION_POLICY
                and g.get("cross_region_status") in ("pending", "unknown_ack")
                and str(p.ticket) not in d.exits
            ):
                continue
            if self._activate_one(p, rec, g):
                changed += 1
        return changed

    def _cross_quote(self, p, g):
        d = self.d
        info = d.mt.symbol_info(p.symbol)
        if info is None or getattr(info, "chart_mode", None) != getattr(
            d.mt, "SYMBOL_CHART_MODE_BID", 0
        ):
            raise Block("cross_stop_chart_unverified")
        grid = Grid.from_info(info)
        if grid.tick != g.get("stop_tick_size", grid.tick):
            raise Block("cross_stop_grid_changed")
        q = d.mt.symbol_info_tick(p.symbol)
        if (
            not fresh(q, d.now(), d.lim.max_quote_age_s)
            or q.time_msc < g["observed_msc"]
        ):
            raise Block("cross_stop_quote_unavailable")
        return grid, q

    def _terms(self, p, rec, g, grid, q):
        """One possible SL change. No adoption of external brackets/uncertain ladder intents."""
        d, key, side = self.d, str(p.ticket), g["side"]
        if (
            d.owners.origin(p) is not rec
            or not d.owned(p)
            or side not in (-1, 1)
            or p.type != d.typ(side)
        ):
            raise Block("cross_stop_identity_unverified")
        if d.kill.exists() or d.state.get("manual_hold") or key in d.exits:
            raise Block("cross_stop_safety_hold")
        ladder = d.ladders.book.get(key)
        if ladder is not None and (
            not d.ladders.same_position(p, key, ladder)
            or ladder.get("pending")
            or ladder.get("hold")
            or ladder.get("broker_exit_seen")
            or not matches_levels(p, pair(ladder), grid)
            or q.time_msc < ladder["confirmed_tick_msc"]
        ):
            raise Block("cross_stop_ladder_unreconciled")
        mark = q.bid if side == 1 else q.ask
        if not (
            p.sl > 0
            and p.tp > 0
            and side * (mark - p.sl) > 0
            and side * (p.tp - mark) > 0
        ):
            raise Block("cross_stop_existing_bracket_unverified")
        raw = grid.price(grid.index(g["hull_cross_price"], side == -1))
        wanted, buffer = spread_adjusted_stop(grid, side, raw, q.bid, q.ask)
        gap = max(grid.gap, grid.tick) - grid.tick * 1e-6
        if (
            side * (wanted - p.sl) > grid.tick * 1e-6
            and min(side * (mark - wanted), side * (p.tp - mark)) < gap
        ):
            raise Block("cross_stop_inside_broker_distance")
        return {
            "sl": wanted,
            "tp": float(p.tp),
            "previous_sl": float(p.sl),
            "volume": float(p.volume),
            "raw": raw,
            "buffer": buffer,
            "grid_tick": grid.tick,
            "managed": ladder is not None,
            "tick_msc": int(q.time_msc),
        }

    def _confirm_stop(self, p, rec, g, grid, q, code=None):
        """Reconcile the exact sent bracket, not a level repriced on today's spread."""
        d, key = self.d, str(p.ticket)
        intent = g.get("cross_stop_intent")
        ladder = d.ladders.book.get(key)
        if not intent or (
            d.owners.origin(p) is not rec
            or not d.owned(p)
            or grid.tick != intent["grid_tick"]
            or p.volume != intent["volume"]
            or p.tp != intent["tp"]
            or abs(p.sl - intent["sl"]) > grid.tick * 1e-6
            or (ladder is not None) != intent["managed"]
        ):
            return False
        if ladder is not None:
            if (
                not d.ladders.same_position(p, key, ladder)
                or ladder.get("pending")
                or ladder.get("hold")
                or ladder.get("broker_exit_seen")
                or abs(ladder["sl"] - intent["previous_sl"]) > grid.tick * 1e-6
                or ladder["tp"] != intent["tp"]
            ):
                return False
            # A verified stop-only change is not a ladder rung or a new risk basis.
            ladder.update(
                sl=float(p.sl),
                stop_spread_buffer=intent["buffer"],
                confirmed_tick_msc=max(
                    ladder["confirmed_tick_msc"], intent["tick_msc"]
                ),
            )
        g.update(
            cross_region_raw_stop=intent["raw"],
            raw_chart_sl=intent["raw"],
            cross_region_action="cross_region_tightened",
            cross_region_status="active",
        )
        del g["cross_stop_intent"]
        d.persist()  # guard and manager move together after exact position read-back
        self._activation_event(p, g, "active", code, q)
        return True

    def _activate_one(self, p, rec, g):
        d, key = self.d, str(p.ticket)
        cross, side = g.get("hull_cross_price"), g["side"]
        if type(cross) not in (int, float) or not math.isfinite(cross) or cross <= 0:
            self._wait(p, g, "cross_stop_intersection_unavailable")
            return False
        intent = None
        try:
            grid, q = self._cross_quote(p, g)
            if side * (q.bid - cross) <= 0:
                self._reserve_cross_exit(p, rec, g, int(q.time_msc))
                return False
            if g.get("cross_region_status") == "unknown_ack":
                if self._confirm_stop(p, rec, g, grid, q):
                    return True
                raise Block(
                    "cross_stop_ack_unverified"
                )  # including old intents with no exact levels
            if q.time_msc <= g.get("cross_stop_refused_msc", -1):
                return False  # definitely refused earlier; retry only on a distinct legal quote
            intent = self._terms(p, rec, g, grid, q)
            if side * (intent["sl"] - p.sl) <= grid.tick * 1e-6:
                g.update(
                    cross_region_raw_stop=intent["raw"],
                    cross_region_action="existing_base_stop_retained",
                    cross_region_status="retained",
                )
                d.persist()
                self._activation_event(p, g, "retained", None, q)
                return True
            req = {
                "action": d.mt.TRADE_ACTION_SLTP,
                "symbol": p.symbol,
                "position": p.ticket,
                "sl": intent["sl"],
                "tp": intent["tp"],
                "magic": d.h.magic,
            }
            check = d.mt.order_check(req)
            if check is None or getattr(check, "retcode", None) != 0:
                raise Block("cross_stop_order_check_rejected")
            # No request has been sent. Re-read identity, BOTH levels and the executable
            # quote after the slow pre-check. Never overwrite a concurrent tighter SL/TP.
            g.update(cross_region_status="unknown_ack", cross_stop_intent=intent)
            d.persist()  # crash from here on: reconcile ONLY this exact intent
            d.account()
            ps = d.mt.positions_get(ticket=p.ticket)
            if ps is None or len(ps) != 1 or d.owners.origin(ps[0]) is not rec:
                raise Block("cross_stop_ticket_changed_before_send")
            curr, last = self._cross_quote(ps[0], g)
            if last.time_msc < q.time_msc or curr != grid:
                raise Block("cross_stop_quote_or_grid_changed")
            if side * (last.bid - cross) <= 0:
                # No SLTP send; this ticket now needs an exit.
                del g["cross_stop_intent"]
                self._reserve_cross_exit(ps[0], rec, g, int(last.time_msc))
                return False
            live = self._terms(ps[0], rec, g, curr, last)
            if live != (intent | {"tick_msc": live["tick_msc"]}):
                raise Block("cross_stop_terms_changed_before_send")
        except Block as exc:
            if intent is not None and g.get("cross_stop_intent") is intent:
                del g["cross_stop_intent"]  # definitely no order_send on THIS attempt
                g["cross_region_status"] = "pending"
                d.persist()
            self._wait(p, g, str(exc))
            return False
        result = d.mt.order_send(req)
        code = getattr(result, "retcode", None)
        d.account()
        ps = d.mt.positions_get(ticket=int(key))
        if (
            ps is not None
            and len(ps) == 1
            and self._confirm_stop(ps[0], rec, g, grid, last, code)
        ):
            return True
        refused = {
            getattr(d.mt, n)
            for n in (
                "TRADE_RETCODE_REJECT",
                "TRADE_RETCODE_INVALID_STOPS",
                "TRADE_RETCODE_FROZEN",
                "TRADE_RETCODE_PRICE_CHANGED",
                "TRADE_RETCODE_TOO_MANY_REQUESTS",
            )
            if hasattr(d.mt, n)
        }
        if (
            code in refused
            and ps is not None
            and len(ps) == 1
            and d.owners.origin(ps[0]) is rec
            and (ps[0].sl, ps[0].tp, ps[0].volume)
            == (intent["previous_sl"], intent["tp"], intent["volume"])
        ):
            del g["cross_stop_intent"]
            g.update(
                cross_region_status="pending", cross_stop_refused_msc=int(last.time_msc)
            )
            d.persist()
            self._wait(p, g, "cross_stop_broker_refused", result)
            return False
        self._wait(p, g, "cross_stop_ack_unverified", result)
        return False

    def _wait(self, p, g, reason, result=None):
        d = self.d
        changed = g.get("cross_region_last_reason") != reason
        g["cross_region_last_reason"] = reason
        if g.get("cross_region_status") != "unknown_ack":
            g["cross_region_status"] = "pending"
        if changed:
            d.persist()
            self._activation_event(
                p, g, "waiting", getattr(result, "retcode", None),
                d.mt.symbol_info_tick(p.symbol), reason=reason,
            )

    def _reserve_cross_exit(self, p, rec, g, stamp):
        d = self.d
        g.update(cross_region_status="exit_pending", cross_region_exit_msc=stamp)
        d._reserve_exit(
            p, rec["tf"], "origin_cross_uncrossed", stamp, child=rec["trigger_tf"]
        )
        d.persist()
        self._activation_event(
            p, g, "exit_pending", None, d.mt.symbol_info_tick(p.symbol),
            reason="quote_returned_through_cross_after_origin_close",
        )
        d._service_exit(p, d.exits[str(p.ticket)])

    def _activation_event(self, p, g, status, retcode, q, *, reason=None):
        self.d.emit(
            "cross_region_stop_activation",
            symbol=p.symbol,
            ticket=p.ticket,
            entry_deal=g.get("entry_deal"),
            parent_tf=g.get("parent_tf"),
            trigger_tf=g.get("trigger_tf"),
            cross_region_policy=g.get("cross_region_policy"),
            hull_cross_price=g.get("hull_cross_price"),
            cross_region_candle_close=g.get("cross_region_candle_close"),
            cross_region_raw_stop=g.get("cross_region_raw_stop"),
            cross_region_action=g.get("cross_region_action"),
            raw_chart_sl=g.get("raw_chart_sl"),
            cross_region_status=status,
            broker_sl=p.sl,
            broker_tp=p.tp,
            reason=reason,
            retcode=retcode,
            bid=getattr(q, "bid", None),
            ask=getattr(q, "ask", None),
        )

    def baseline_opposite(self, name, parent, r):
        d = self.d
        if not d.armed or not d.open_lanes:
            return 0
        start = (r.close_s - r.tf * 60) * 1000
        matches = []
        for rec in d.owners.book.values():
            if (
                rec.get("intrabar_origin")
                and rec["symbol"] == name
                and rec["magic"] == d.h.magic
                and rec.get("missing_since") is None
                and rec["opened_msc"] < start
                and rec["type"] != d.typ(r.label)
                and (
                    rec["tf"] == r.tf
                    or (rec["tf"] == parent and rec["trigger_tf"] == r.tf)
                )
            ):
                matches.append(rec)
        if not matches:
            return 0
        # Persist even when MT5 cannot verify inventory until a later sweep.
        for rec in matches:
            rec.setdefault("recovered_opposite_msc", r.signal_msc)
        d.persist()
        active = []
        for p in d.mt.positions_get() or ():
            if d.owners.origin(p) in matches and self.reserve(p):
                active.append(p)
        if active:
            d.persist()
        for p in active:
            d._service_exit(p, d.exits[str(p.ticket)])
        return len(active)

    def reserve(self, p):
        d = self.d
        rec = d.owners.origin(p) if d.owned(p) else None
        g = (rec or {}).get("intrabar_origin")
        if g and g["status"] == "invalid":
            reason, stamp = "origin_cross_uncrossed", g["observed_msc"]
        elif g and g.get("cross_region_status") == "exit_pending":
            reason, stamp = "origin_cross_uncrossed", g["cross_region_exit_msc"]
        elif g and rec.get("recovered_opposite_msc"):
            reason, stamp = "verified_opposite_hull", rec["recovered_opposite_msc"]
        else:
            return False
        d._reserve_exit(p, rec["tf"], reason, stamp, child=rec["trigger_tf"])
        return True
