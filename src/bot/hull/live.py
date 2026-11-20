"""Sample a broker symbol's current BID candle(s). No backfill, background orders or closed-state writes."""

from __future__ import annotations

from bot.zones.engine import Bar

from .signal import LiveTick


def current_bar(mt, name, h, tick):
    """Index zero is ONLY a projection input. Reject rollovers, LAST charts, and mismatched snapshots.

    The caller checks chart mode once and brackets these reads with the same broker tick.
    Never replace a stale close with a newer bid: the high/low might be from the wrong snapshot.
    """
    period = h.tf * 60
    start = h.prev.t + period
    if not start * 1000 <= tick.ms < (start + period) * 1000:
        return None
    rates = mt.copy_rates_from_pos(name, getattr(mt, f"TIMEFRAME_M{h.tf}"), 0, 1)
    if rates is None or len(rates) != 1:
        return None
    r = rates[0]
    try:
        if int(r["time"]) != start or r["close"] != tick.bid:
            return None
        return Bar(start, *(float(r[k]) for k in ("open", "high", "low", "close")))
    except (KeyError, IndexError, ValueError, TypeError, OverflowError):
        return None


class LiveSampler:
    """One latch per native lane/bar, one common tick for multi-lane entry arbitration."""

    def __init__(self, lanes, emit):
        self.lanes = lanes  # tf -> Stream
        self.emit = emit
        self.skip_through = 0.0
        self.disconnected = True
        self.used = {}
        self.conflicts = {}
        self.last_tick = None
        self.last_poll = None
        self.last_status = ""

    def reset(self, now):
        # Never interpret a cross already in progress when attaching/reconnecting as new.
        self.skip_through = max(self.skip_through, now)
        self.disconnected = False
        self.last_tick = self.last_poll = None

    def _status(self, name, why):
        if self.last_status != why:
            self.emit("live_sample_skipped", symbol=name, reason=why)
            self.last_status = why

    def watching(self):
        return any(s.h.structure.view.continuation_side for s in self.lanes.values())

    def sample(self, mt, name, clock, max_age):
        now = clock()
        if self.disconnected or (
            self.last_poll is not None and not 0 <= now - self.last_poll <= max_age
        ):
            self.reset(now)
        self.last_poll = now if self.watching() else None
        wanted = {}
        for tf, lane in self.lanes.items():
            h = lane.h
            if h.prev is None or not h.structure.view.continuation_side:
                continue
            start = h.prev.t + tf * 60
            if (
                self.skip_through < start <= now < start + tf * 60
                and self.used.get(tf) != start
            ):
                wanted[tf] = h
        if not wanted:
            return []
        tick = LiveTick.read(mt.symbol_info_tick(name))
        now = clock()
        if (
            tick is None
            or not 0 <= now - tick.ms / 1000 <= max_age
            or (self.last_tick and tick.ms < self.last_tick.ms)
        ):
            self._status(name, "stale_invalid_or_regressed_live_quote")
            self.disconnected = True
            return []
        # A previous-bar tick during ordinary rollover is not a new live quote.
        wanted = {
            tf: h for tf, h in wanted.items() if tick.ms >= (h.prev.t + tf * 60) * 1000
        }
        if not wanted or tick == self.last_tick:
            return []
        bars = {tf: current_bar(mt, name, h, tick) for tf, h in wanted.items()}
        if LiveTick.read(mt.symbol_info_tick(name)) != tick:
            # A new tick arrived during the native reads. Never join its time/price
            # to the older OHLC, or combine timeframes from different ticks.
            return []
        if any(b is None for b in bars.values()):
            self._status(name, "unverified_native_bid_candle")
            self.disconnected = True
            return []
        self.last_tick = tick
        self.last_status = ""
        out = []
        now = clock()
        for tf, h in wanted.items():
            b = bars[tf]
            if (
                not b.t <= now < b.t + tf * 60
                or not 0 <= now - tick.ms / 1000 <= max_age
            ):
                continue
            r = h.preview(b, tick)
            if r.source_conflict and self.conflicts.get(tf) != b.t:
                self.conflicts[tf] = b.t
                self.emit(
                    "live_source_cross_conflict", symbol=name, tf=tf, **r.clock_fields()
                )
            if r.label == h.structure.view.continuation_side:
                self.used[tf] = (
                    b.t
                )  # latch BEFORE any safety veto, never rearm on a flutter
                out.append(r)
        return out

    def still_crossed(self, mt, name, r, q):
        """A repriced batch leg must re-prove the cross, not merely the frozen structure band."""
        h = self.lanes[r.tf].h
        tick = LiveTick.read(q)
        if not tick or h.prev.t + 2 * r.tf * 60 != r.close_s:
            return False
        b = current_bar(mt, name, h, tick)
        return (
            b is not None
            and LiveTick.read(mt.symbol_info_tick(name)) == tick
            and h.preview(b, tick).entry_side == r.side
        )
