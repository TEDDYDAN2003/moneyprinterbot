"""Ticket-scoped invalidation of a live entry, including its actual closing print.

The two curves on the entry candle have a fixed previous relationship. The
original cross still exists iff B is strictly on its entry side of that SAME
source A. A label on the following candle is not evidence about this one.
"""

import math

from .config import INTRA_ENTRY


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

    def observe(self, name, parent, r):
        changed = []
        stamp = r.signal_msc
        for rec, g in self.watching(name, parent, r.tf):
            if g["end"] != r.close_s or stamp < g["ms"]:
                continue
            holds = relation(r, g["source"], g["side"])
            if holds is None or (holds and r.trigger is not None):
                continue
            g.update(status="confirmed" if holds else "invalid", observed_msc=stamp)
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
        elif g and rec.get("recovered_opposite_msc"):
            reason, stamp = "verified_opposite_hull", rec["recovered_opposite_msc"]
        else:
            return False
        d._reserve_exit(p, rec["tf"], reason, stamp, child=rec["trigger_tf"])
        return True
