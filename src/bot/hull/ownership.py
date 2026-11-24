"""Execution-time attribution, independent of comments, display charts and most recent signal.

The account-specific journal is authoritative only after the broker opening order
and position are reconciled. Legacy/unknown tickets are NEVER assigned by guess.
"""

from .ladder_desk import opened_ms


def fingerprint(p):
    return {
        "symbol": p.symbol,
        "magic": int(p.magic),
        "ticket": int(p.ticket),
        "identifier": int(getattr(p, "identifier", p.ticket)),
        "type": int(p.type),
        "opened_msc": opened_ms(p),
        "open": float(p.price_open),
    }


class Owners:
    def __init__(self, state):
        self.book = state.setdefault("hull_lane_positions_v1", {})

    def bind(self, p, tf, *, minute_source=None, minute_policy=None):
        record = fingerprint(p) | {"tf": int(tf)}
        if minute_source is not None:
            record.update(
                trigger_tf=1, m1_source=minute_source, m1_policy_key=minute_policy
            )
        if (
            record["opened_msc"] <= 0
            or record["identifier"] <= 0
            or record["ticket"] <= 0
        ):
            return False
        key = str(record["identifier"])
        if key in self.book and self.book[key] != record:
            return False
        self.book[key] = record
        return True

    def lane(self, p):
        rec = self.book.get(str(getattr(p, "identifier", p.ticket)))
        return (
            rec["tf"]
            if rec and all(rec.get(k) == v for k, v in fingerprint(p).items())
            else None
        )

    def minute_source(self, p):
        # No inference from broker comments, current chart or magic alone.
        if self.lane(p) is None:
            return None
        rec = self.book[str(getattr(p, "identifier", p.ticket))]
        return rec.get("m1_source") if rec.get("trigger_tf") == 1 else None

    def deal_lane(self, deal, symbols, magic):
        """Read-only audit link, never used to admit, close or enroll a position."""
        pid = getattr(deal, "position_id", 0)
        rec = self.book.get(str(pid))
        ms = getattr(deal, "time_msc", None)
        if not (
            rec
            and pid
            and rec.get("identifier") == pid
            and rec.get("symbol") == deal.symbol
            and deal.symbol in symbols
            and rec.get("magic") == magic
            and isinstance(ms, int)
            and 0 < (rec.get("opened_msc") or 0) <= ms
            and rec.get("type") in (0, 1)
        ):
            return None
        if deal.entry == 0 and deal.magic == magic and deal.type == rec["type"]:
            return rec.get("tf")
        if (
            deal.entry in (1, 3) and deal.type == 1 - rec["type"]
        ):  # OUT / OUT_BY (including manual)
            return rec.get("tf")
        return None

    def prune(self, active_ids, now):
        # Keep recently closed IDs to attribute manual/SL exits in the bounded
        # post-signal deals window even when the closing deal's magic is zero.
        dirty = False
        for key, rec in list(self.book.items()):
            if key in active_ids:
                if "missing_since" in rec:
                    del rec["missing_since"]
                    dirty = True
            elif "missing_since" not in rec:
                rec["missing_since"] = now
                dirty = True
            elif now - rec["missing_since"] > 86400:
                del self.book[key]
                dirty = True
        return dirty
