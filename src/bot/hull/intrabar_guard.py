"""Ticket-scoped invalidation of a live entry, including its actual closing print.

The two curves on the entry candle have a fixed previous relationship. The
original cross still exists iff B is strictly on its entry side of that SAME
source A. A label on the following candle is not evidence about this one.
"""

import math

from bot.broker import fresh

from .config import CROSS_REGION_POLICY, INTRA_ENTRY
from .orders import Grid, spread_adjusted_stop


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

    def observe(self, name, parent, r):
        changed = []
        stamp = r.signal_msc
        for rec, g in self.watching(name, parent, r.tf):
            if g["end"] != r.close_s or stamp < g["ms"]:
                continue
            holds = relation(r, g["source"], g["side"])
            cross = g.get("hull_cross_price")
            cross_close_ok = (
                cross is None or g["side"] * (r.close - cross) > 0
            )
            if holds is None or (holds and r.trigger is not None):
                continue
            confirmed = holds and cross_close_ok
            g.update(
                status="confirmed" if confirmed else "invalid",
                observed_msc=stamp,
            )
            if confirmed and g.get("cross_region_policy") == CROSS_REGION_POLICY:
                g.update(
                    cross_region_candle_close=r.close,
                    cross_region_status="pending",
                )
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

    def _activate_one(self, p, rec, g):
        d = self.d
        side = 1 if p.type == d.typ(1) else -1
        if (
            rec.get("tf") is None
            or rec.get("trigger_tf") is None
            or rec.get("symbol") != p.symbol
            or rec.get("identifier") != getattr(p, "identifier", p.ticket)
            or rec.get("ticket") != p.ticket
            or rec.get("type") != p.type
            or g.get("side") != side
            or not p.sl > 0
            or not p.tp > 0
        ):
            self._wait(p, g, "cross_stop_identity_or_protection_unverified")
            return False
        q = d.mt.symbol_info_tick(p.symbol)
        if not fresh(q, d.now(), d.lim.max_quote_age_s):
            self._wait(p, g, "cross_stop_quote_unavailable")
            return False
        cross = g.get("hull_cross_price")
        if not isinstance(cross, (int, float)) or not math.isfinite(cross) or cross <= 0:
            self._wait(p, g, "cross_stop_intersection_unavailable")
            return False
        if side * (q.bid - cross) <= 0:
            self._reserve_cross_exit(p, rec, g, int(q.time_msc))
            return False
        info = d.mt.symbol_info(p.symbol)
        if info is None:
            self._wait(p, g, "cross_stop_symbol_info_unavailable")
            return False
        try:
            grid = Grid.from_info(info)
            raw = grid.price(grid.index(cross, side == -1))
            wanted, _ = spread_adjusted_stop(grid, side, raw, q.bid, q.ask)
        except (ValueError, ArithmeticError, TypeError):
            self._wait(p, g, "cross_stop_price_unavailable")
            return False
        mark = q.bid if side == 1 else q.ask
        if side * (mark - wanted) < max(grid.gap, grid.tick) - grid.tick * 1e-6:
            self._wait(p, g, "cross_stop_inside_broker_distance")
            return False
        tol = grid.tick * 1e-6
        already_tighter = wanted <= p.sl + tol if side == 1 else wanted >= p.sl - tol
        g["cross_region_raw_stop"] = raw
        if (
            g.get("cross_region_status") == "unknown_ack"
            and abs(p.sl - wanted) <= tol
        ):
            g.update(
                cross_region_action="cross_region_tightened",
                raw_chart_sl=raw,
                cross_region_status="active",
            )
            d.persist()
            self._activation_event(p, g, "active", None, q)
            return True
        if already_tighter:
            g.update(
                cross_region_action="existing_base_stop_retained",
                cross_region_status="retained",
            )
            d.persist()
            self._activation_event(p, g, "retained", None, q)
            return True
        if g.get("cross_region_status") == "unknown_ack":
            return False  # inspect ambiguous stop acks; never blindly resend
        req = {
            "action": d.mt.TRADE_ACTION_SLTP,
            "symbol": p.symbol,
            "position": p.ticket,
            "sl": wanted,
            "tp": p.tp,
            "magic": d.h.magic,
        }
        check = d.mt.order_check(req)
        if check is None or getattr(check, "retcode", None) != 0:
            self._wait(p, g, "cross_stop_order_check_rejected")
            return False
        g["cross_region_status"] = "unknown_ack"
        d.persist()  # write ahead; the stop change is reconciled against this ticket
        d.account()
        result = d.mt.order_send(req)
        after = d.mt.positions_get(ticket=p.ticket)
        verified = (
            result is not None
            and getattr(result, "retcode", None) == d.mt.TRADE_RETCODE_DONE
            and after is not None
            and len(after) == 1
            and d.owned(after[0])
            and after[0].ticket == p.ticket
            and getattr(after[0], "identifier", after[0].ticket)
            == rec["identifier"]
            and after[0].type == p.type
            and after[0].price_open == p.price_open
            and after[0].volume == p.volume
            and abs(after[0].tp - p.tp) <= tol
            and abs(after[0].sl - wanted) <= tol
        )
        if verified:
            g.update(
                cross_region_raw_stop=raw,
                cross_region_action="cross_region_tightened",
                raw_chart_sl=raw,
                cross_region_status="active",
            )
            d.persist()
            self._activation_event(p, g, "active", getattr(result, "retcode", None), q)
            return True
        self._wait(p, g, "cross_stop_ack_unverified", result)
        return False

    def _wait(self, p, g, reason, result=None):
        d = self.d
        changed = g.get("cross_region_last_reason") != reason
        g["cross_region_last_reason"] = reason
        if g.get("cross_region_status") != "unknown_ack":
            g["cross_region_status"] = "pending"
        d.persist()
        if changed:
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
