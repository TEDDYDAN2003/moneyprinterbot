"""Port of the supplied Free Chart Hull calculation. Commit only finished OHLC; live projections are side-effect free."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field, replace
from itertools import chain, islice

from bot.zones.engine import Bar

from .config import FRAMES, Indicator
from .entry_clock import utc
from .structure import ENTRY_POLICY, InternalStructure, Pivot, Structure

ENTRY_AUDIT_FIELDS = (
    "entry_policy",
    "protected_level",
    "pullback_candidate",
    "pullback_seen_close_s",
    "zone_low_open",
    "zone_high_open",
    "structure_phase",
    "entry_setup",
    "zone_low",
    "zone_high",
    "entry_side",
    "entry_reason",
)


class WMA:
    def __init__(self, n):
        self.q = deque(maxlen=n)
        self.n = n
        self.div = n * (n + 1) / 2

    def next(self, v, commit):
        if commit:
            if v is None:
                self.q.clear()
                return None
            self.q.append(v)
            if len(self.q) != self.n:
                return None
            terms = self.q
        else:
            if v is None or len(self.q) + 1 < self.n:
                return None
            # Same multiply/add order as push, without modifying the closed buffer.
            terms = chain(islice(self.q, int(len(self.q) == self.n), None), (v,))
        weighted = 0.0
        for i, x in enumerate(terms, 1):
            weighted += i * x
        return weighted / self.div


class Baseline:
    """One source's entire A history. Previous B is shared; previous A is not."""

    def __init__(self, half, length):
        self.fast, self.slow = WMA(half), WMA(length)
        self.smooth = WMA(max(1, math.floor(math.sqrt(length) + 0.5)))
        self.last = None

    def update(self, source, b, prev_b, commit):
        x, y = self.fast.next(source, commit), self.slow.next(source, commit)
        a = self.smooth.next(
            2 * x - y if x is not None and y is not None else None, commit
        )
        prev_a = self.last
        if commit:
            self.last = a
        cross = 0
        if None not in (a, b, prev_a, prev_b):
            if b > a and prev_b < prev_a:
                cross = 1
            elif b < a and prev_b > prev_a:
                cross = -1
        return a, cross


@dataclass(frozen=True, slots=True)
class LiveTick:
    ms: int
    bid: float
    ask: float

    @classmethod
    def read(cls, q):
        if q is None:
            return None
        ms = getattr(q, "time_msc", None)
        b, a = getattr(q, "bid", None), getattr(q, "ask", None)
        if not (
            isinstance(ms, int)
            and ms > 0
            and isinstance(b, (float, int))
            and isinstance(a, (float, int))
            and 0 < b <= a
            and math.isfinite(a)
        ):
            return None
        return cls(ms, b, a)


@dataclass(frozen=True, slots=True)
class Reading:
    close_s: int  # scheduled candle end; NOT the trigger time on a live projection
    tf: int
    close: float
    a: float | None
    b: float | None
    ema: float
    atr: float | None
    label: int
    side: int
    reason: str
    anchor: float | None
    anchor_s: int | None
    raw_sl: float | None
    window_start_s: int | None
    prior_bars: int
    pivot_low: float | None
    pivot_high: float | None
    # Empty history fails closed; it never implies permission to send.
    structure: Structure = field(default_factory=Structure)

    # `a` is meaningful in single-source mode only. Never invent a blended A for Either.
    a_low: float | None = None
    a_high: float | None = None
    cross_low: int = 0
    cross_high: int = 0
    source_conflict: bool = False

    trigger: LiveTick | None = None
    confirmation_only: bool = (
        False  # chart cross whose continuation belonged to the live path
    )

    @property
    def signal_msc(self):
        return self.trigger.ms if self.trigger else self.close_s * 1000

    @property
    def signal_s(self):
        return self.trigger.ms / 1000 if self.trigger else self.close_s

    def clock_fields(self):
        return {
            "entry_timing": "live_hull_cross" if self.trigger else "confirmed_close",
            "trigger_msc": self.signal_msc,
            "trigger_utc": utc(self.signal_s),
            "bar_open_utc": utc(self.close_s - self.tf * 60),
            "bar_end_utc": utc(self.close_s),
            "signal_close_utc": None if self.trigger else utc(self.close_s),
        }

    @property
    def entry_reason(self) -> str:
        if not self.label:
            return "source_conflict" if self.source_conflict else "no_cross"
        if not self.side:
            return self.reason if self.reason != "ready" else "no_executable_label"
        if self.side != self.label:
            return "no_matching_hull_label"
        if self.trigger:
            if self.side != self.structure.bias:
                return "opposite_label_requires_close"
            seen = self.structure.pullback_seen_s
            if (
                not self.structure.continuation_side
                or seen is None
                or seen > self.close_s - self.tf * 60
            ):
                return "live_requires_completed_pullback"
        return self.structure.veto(self.side, self.close) or (
            "continuation_live_only" if self.confirmation_only else "ready"
        )

    @property
    def entry_side(self) -> int:
        # `side` intentionally remains the pre-structure side used by existing exits.
        return self.side if self.entry_reason == "ready" else 0

    def entry_audit(self):
        band = self.structure.band(self.label)
        return {
            "entry_policy": ENTRY_POLICY,
            "protected_level": self.structure.origin.price
            if self.structure.origin
            else None,
            "pullback_candidate": self.structure.pullback.price
            if self.structure.pullback
            else None,
            "pullback_seen_close_s": self.structure.pullback_seen_s,
            "zone_low_open": band.low_open if band else None,
            "zone_high_open": band.high_open if band else None,
            "structure_phase": self.structure.phase,
            "entry_setup": band.name if band else None,
            "zone_low": band.low if band else None,
            "zone_high": band.high if band else None,
            "entry_side": self.entry_side,
            "entry_reason": self.entry_reason,
        }


class Hull:
    def __init__(self, tf: int, p: Indicator, pip_size: float):
        if tf not in FRAMES:
            raise ValueError("Hull timeframes are M1, M2, M5 only")
        self.tf, self.p, self.pip = tf, p, pip_size
        n = p.hull_len
        h = max(1, n // 2)
        self.baselines = {
            src: Baseline(h, n)
            for src in (("low", "high") if p.source == "either" else (p.source,))
        }
        self.b_weights = (WMA(max(1, h // 3)), WMA(max(1, h // 2)), WMA(h), WMA(h))
        self.prev = None
        self.last_b = self.atr = self.ema = None
        self.n = 0
        self.tr_sum = 0.0
        self.hist = deque(maxlen=max(2 * p.swing_len + 1, p.pullback_bars + 1))
        self.low = self.high = None
        self.structure = InternalStructure()
        self.last_cross = {1: None, -1: None}

    def update(self, c: Bar) -> Reading:
        if self.prev and c.t <= self.prev.t:
            raise ValueError("Closed bars must be unique and ordered")
        return self._read(c, None)

    def preview(self, c: Bar, tick: LiveTick) -> Reading:
        """One projection from the SAME last closed candle on every tick. Never commit it."""
        period = self.tf * 60
        if (
            self.prev is None
            or c.t != self.prev.t + period
            or c.c != tick.bid
            or not c.t * 1000 <= tick.ms < (c.t + period) * 1000
        ):
            raise ValueError(
                "Live bid candle and broker tick must follow the closed state"
            )
        return self._read(c, tick)

    def _read(self, c, tick):
        commit = tick is None
        p = self.p
        values = {
            "open": c.o,
            "high": c.h,
            "low": c.l,
            "close": c.c,
            "hl2": (c.h + c.l) / 2,
            "hlc3": (c.h + c.l + c.c) / 3,
            "ohlc4": (c.o + c.h + c.l + c.c) / 4,
        }
        fast, mid, slow, smooth = self.b_weights
        x, y, z = fast.next(c.c, commit), mid.next(c.c, commit), slow.next(c.c, commit)
        b = smooth.next(3 * x - y - z if None not in (x, y, z) else None, commit)
        curves = {
            src: curve.update(values[src], b, self.last_b, commit)
            for src, curve in self.baselines.items()
        }
        low, low_cross = curves.get("low", (None, 0))
        high, high_cross = curves.get("high", (None, 0))
        conflict = bool(low_cross and high_cross == -low_cross)
        a, label = (
            (None, 0 if conflict else low_cross or high_cross)
            if p.source == "either"
            else curves[p.source]
        )
        ema = (
            c.c
            if self.ema is None
            else self.ema + 2 / (p.ema_length + 1) * (c.c - self.ema)
        )
        prior = list(self.hist)[-p.pullback_bars :] if label else []
        close_s = c.t + self.tf * 60
        if commit:
            self.ema = ema
            tr = (
                c.h - c.l
                if self.prev is None
                else max(c.h - c.l, abs(c.h - self.prev.c), abs(c.l - self.prev.c))
            )
            self.n += 1
            if self.atr is None:
                self.tr_sum += tr
                if self.n == p.atr_length:
                    self.atr = self.tr_sum / p.atr_length
            else:
                self.atr = (self.atr * (p.atr_length - 1) + tr) / p.atr_length
            self.hist.append(c)
            s = p.swing_len
            pl = ph = None
            if len(self.hist) >= 2 * s + 1:
                span = list(self.hist)[-(2 * s + 1) :]
                mid = span[s]
                left = span[:s]
                right = span[s + 1 :]
                if all(mid.l <= q.l for q in left) and all(mid.l < q.l for q in right):
                    self.low = pl = Pivot(mid.l, mid.t, close_s)
                if all(mid.h >= q.h for q in left) and all(mid.h > q.h for q in right):
                    self.high = ph = Pivot(mid.h, mid.t, close_s)
            structure = self.structure.update(c, close_s, ph, pl)
        else:
            # Freeze structure, stop anchors AND ATR at the previous completed bar.
            # A wick cannot confirm a pivot, a pullback, a CHoCH or a BOS here.
            structure = self.structure.view
            if structure.break_now:
                structure = replace(structure, break_now=False)
        anchor = when = sl = window = None
        count = 0
        side = 0
        reason = "source_conflict" if conflict else "no_cross"
        if label:
            if p.stop == "local_pullback":
                since = self.last_cross[-label]
                prior = [q for q in prior if since is None or q.t >= since]
                if prior:
                    # Most recent equal wick wins. Signal candle is deliberately excluded.
                    pick = (
                        min(prior, key=lambda q: (q.l, -q.t))
                        if label == 1
                        else max(prior, key=lambda q: (q.h, q.t))
                    )
                    anchor, when = (pick.l if label == 1 else pick.h), pick.t
                    window = prior[0].t
                    count = len(prior)
            else:
                chosen = self.low if label == 1 else self.high
                if chosen:
                    anchor, when = chosen.price, chosen.bar_s
            if anchor is not None and self.atr is not None:
                sl = anchor - label * (
                    p.atr_buffer * self.atr + p.extra_pips * self.pip
                )
            if sl is None:
                reason = "no_known_swing_or_atr"
            elif sl <= 0 or label * (c.c - sl) <= 0:
                reason = (
                    "swing_already_lost_at_close"
                    if commit
                    else "swing_already_lost_at_trigger"
                )
            elif p.signal == "ema_plan" and label * (c.c - ema) <= 0:
                reason = "ema_filter"
            else:
                side = label
                reason = "ready"
            if commit:
                self.last_cross[label] = c.t
        if commit:
            self.prev, self.last_b = c, b
        return Reading(
            close_s,
            self.tf,
            c.c,
            a,
            b,
            ema,
            self.atr,
            label,
            side,
            reason,
            anchor,
            when,
            sl,
            window,
            count,
            self.low.price if self.low else None,
            self.high.price if self.high else None,
            structure,
            low,
            high,
            low_cross,
            high_cross,
            conflict,
            tick,
        )
