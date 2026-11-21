"""Native MT5 closed-rate clock plus verified, non-committing bid-chart projections."""

from __future__ import annotations

from bisect import bisect_right
from math import ceil

from bot.execution.demo_runner import DemoSafetyError
from bot.zones.engine import Bar

from .config import TF_NAME
from .curves import LiveTick
from .signal import Hull
from .structure import InternalStructure, Structure


def rates(mt, name, tf, pos, count):
    period = getattr(mt, "TIMEFRAME_" + TF_NAME[tf])
    return mt.copy_rates_from_pos(name, period, pos, count)


def candle(r):
    return Bar(int(r["time"]), *(float(r[k]) for k in ("open", "high", "low", "close")))


class StructureTape:
    """One independent native structure context per symbol with a TF override.

    Higher TF: last native close at/before chart OPEN. Lower TF: last finished
    native close at/before chart close or trigger tick. Never project a forming
    structure bar. The tail must cover the chart's pullback-stop lookback.
    """

    def __init__(self, tf, depth, max_chart_tf, stop_bars):
        self.tf, self.depth = tf, depth
        self.keep = 1200 + ceil((stop_bars + 4) * max_chart_tf / tf)
        self.required = 2 * depth + 2 + ceil((stop_bars + 2) * max_chart_tf / tf)
        self.engine = InternalStructure(depth)
        self.ends, self.views = [], []
        self.ready = False

    def refresh(self, mt, name, now):
        step = self.tf * 60
        reload = not self.ready or not self.ends or now - self.ends[-1] > 3 * step
        rows = rates(mt, name, self.tf, 1, self.keep if reload else 4)
        if rows is None or len(rows) < (self.required if reload else 1):
            self.ready = False
            return "history_unavailable"
        if reload:
            self.engine, self.ends, self.views = InternalStructure(self.depth), [], []
        last, caught_up, gap = -1, 0, False
        for row in rows:
            t = int(row["time"])
            if t <= last:
                raise DemoSafetyError(
                    f"{name} {TF_NAME[self.tf]}: unordered structure history"
                )
            last = t
            if t + step > now or (self.ends and t + step <= self.ends[-1]):
                continue
            gap |= bool(self.ends and t != self.ends[-1])
            view = self.engine.update(candle(row), t + step)
            self.ends.append(t + step)
            self.views.append(view)
            caught_up += 1
        if len(self.ends) > self.keep + 128:
            self.ends, self.views = self.ends[-self.keep :], self.views[-self.keep :]
        self.ready = len(self.ends) >= self.required
        if not self.ready:
            return "history_unavailable"
        return "warmup_only" if reload or gap or caught_up > 1 else "updated"

    def at(self, chart_tf, start, observed):
        cutoff = start if self.tf > chart_tf else observed
        idx = bisect_right(self.ends, cutoff) - 1
        return self.views[idx] if idx >= 0 else Structure()

    def current(self, chart_tf, start, observed):
        v = self.at(chart_tf, start, observed)
        cutoff = start if self.tf > chart_tf else observed
        return (
            self.ready
            and v.closed_s is not None
            and 0 <= cutoff - v.closed_s < self.tf * 60
        )


class Stream:
    def __init__(self, tf, cfg, pip, external: StructureTape | None = None):
        self.tf, self.cfg, self.pip, self.ext = tf, cfg, pip, external
        self.h = Hull(tf, cfg, pip)
        self.last_label = None
        self.observed = {}  # (bar end -> keys); raw notification != entry permission
        self.baseline_due = True
        self.last_sample = self.last_tick_ms = None
        self.min_bars = max(
            cfg.hull_len + int(cfg.hull_len**0.5) + 2,
            cfg.atr_length + 1,
            2 * cfg.swing_len + 2,
            cfg.pullback_bars + 1,
        )

    def refresh(self, mt, name, now):
        step = self.tf * 60
        reload = self.h.prev is None or now - self.h.prev.t > step * 4
        rows = rates(
            mt, name, self.tf, 1, max(self.min_bars + 2, 1200) if reload else 4
        )
        if rows is None or len(rows) < (self.min_bars if reload else 1):
            self.baseline_due = True
            return None, "history_unavailable"
        if reload:
            self.h, self.last_label = (
                Hull(self.tf, self.cfg, self.pip, self.h.gate),
                None,
            )
        updates, last, gap, decision = 0, -1, False, None
        for row in rows:
            t = int(row["time"])
            if t <= last:
                raise DemoSafetyError(
                    f"{name} {TF_NAME[self.tf]}: unordered chart bars"
                )
            last = t
            if t + step > now or (self.h.prev and t <= self.h.prev.t):
                continue
            gap |= bool(self.h.prev and t != self.h.prev.t + step)
            context = self.ext.at(self.tf, t, t + step) if self.ext else None
            decision = self.h.update(
                candle(row),
                context=context,
                observe_first=not self.ext or self.ext.current(self.tf, t, t + step),
            )
            if decision.label:
                self.last_label = decision
            updates += 1
        self.h.gate.flush()
        if reload or gap or updates > 1 or (updates and self.baseline_due):
            self.baseline_due = True
            return None, "warmup_only"
        if (
            decision
            and self.ext
            and not self.ext.current(self.tf, decision.close_s - step, decision.close_s)
        ):
            return None, "history_unavailable"
        return decision, "updated" if decision else "unchanged"

    def current_bar(self, mt, name, tick):
        if not self.h.prev:
            return None
        step = self.tf * 60
        start = self.h.prev.t + step
        if not start * 1000 <= tick.ms < (start + step) * 1000:
            return None
        rows = rates(mt, name, self.tf, 0, 1)
        if rows is None or len(rows) != 1:
            return None
        try:
            b = candle(rows[0])
        except (ValueError, TypeError, OverflowError, IndexError, KeyError):
            return None
        if b.t != start or b.c != tick.bid:
            return None
        return b

    def project(self, mt, name, tick):
        b = self.current_bar(mt, name, tick)
        if b is None or (
            self.ext and not self.ext.current(self.tf, b.t, tick.ms / 1000)
        ):
            return None
        context = self.ext.at(self.tf, b.t, tick.ms / 1000) if self.ext else None
        return self.h.preview(b, tick, context=context)

    def note(self, r, *, baseline=False):
        """One raw event per (chart candle, direction, selected source, bias, MSS).

        A verified cross at attachment is not a new signal. An observed live
        event cannot fire again just because its candle subsequently closes.
        A real cross on a flip is still a raw exit even when entry is skipped.
        """
        self.observed = {
            k: v for k, v in self.observed.items() if k >= r.close_s - self.tf * 60
        }
        if not r.label:
            return False
        keys = self.observed.setdefault(r.close_s, set())
        key = r.label, r.active_source, r.structure.bias, r.structure.epoch
        fresh = key not in keys
        keys.add(key)
        return fresh and not baseline


def sample(mt, name, lanes, clock, max_age):
    """Bracket native candle reads with one unchanged bid/ask/time snapshot.
    A bad lane cannot suppress a protective exit observed on another valid lane.
    """
    q = LiveTick.read(mt.symbol_info_tick(name))
    if not q or not 0 <= clock() - q.ms / 1000 <= max_age:
        return [], False
    readings = [(s, s.project(mt, name, q)) for s in lanes]
    if LiveTick.read(mt.symbol_info_tick(name)) != q:
        return [], False
    now = clock()
    if not 0 <= now - q.ms / 1000 <= max_age:
        return [], False
    out, complete = [], True
    for s, r in readings:
        if not r or now >= r.close_s:
            complete = False
            continue
        if s.last_tick_ms is not None and q.ms < s.last_tick_ms:
            complete = False
            s.baseline_due = True
            continue
        resumed = s.last_sample is not None and not 0 <= now - s.last_sample <= max_age
        r = s.h.gate.apply(r, observe=True)  # coherent broker snapshot only
        s.h.gate.flush()
        if s.note(r, baseline=s.baseline_due or resumed):
            out.append(r)
        s.baseline_due = False
        s.last_tick_ms, s.last_sample = q.ms, now
    return out, complete


def verify_before_leg(mt, name, lanes, signal, quote, clock, max_age, on_opposite):
    """Reprice, don't chase: no next leg after an opposite observed during a batch.

    A fresh opposite seen here is serviced even if it vanishes before the next
    outer poll. It never recursively opens a reversal from inside an order send.
    """
    q = LiveTick.read(quote)
    if not q or not 0 <= clock() - q.ms / 1000 <= max_age:
        return False
    readings = {tf: lane.project(mt, name, q) for tf, lane in lanes.items()}
    now = clock()
    if (
        LiveTick.read(mt.symbol_info_tick(name)) != q
        or not 0 <= now - q.ms / 1000 <= max_age
    ):
        return False
    ok = True
    for tf, r in readings.items():
        lane = lanes[tf]
        if (
            r is None
            or now >= r.close_s
            or (lane.last_tick_ms is not None and q.ms < lane.last_tick_ms)
        ):
            ok = False
            continue
        r = lane.h.gate.apply(r, observe=True)
        lane.h.gate.flush()
        readings[tf] = r
        if r.label == -signal.label and lane.note(r, baseline=lane.baseline_due):
            on_opposite(r)
            ok = False
    r = readings[signal.tf]
    if (
        not ok
        or not r
        or r.structure.bias != signal.label
        or r.structure.epoch != signal.structure.epoch
    ):
        return False
    return not signal.trigger or (
        r.close_s == signal.close_s
        and r.entry_side == signal.entry_side
        and r.active_source == signal.active_source
    )
