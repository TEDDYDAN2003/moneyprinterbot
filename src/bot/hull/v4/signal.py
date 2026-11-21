"""Port of the supplied Free Chart Hull calculation. Commit only finished OHLC; live projections are side-effect free."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from ..curves import Baseline, LiveTick, WMA

from bot.zones.engine import Bar

from .config import FRAMES, Indicator
from ..entry_clock import utc
from .structure import ENTRY_POLICY, InternalStructure, Pivot, Structure

ENTRY_AUDIT_FIELDS = (
    "entry_policy",
    "range_midpoint",
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
        if self.trigger is not None:
            return "closed_cross_required"
        if self.source_conflict:
            return "source_conflict"
        if not self.label:
            return "no_cross"
        if not self.side:
            return self.reason if self.reason != "ready" else "no_executable_label"
        if self.side != self.label:
            return "no_matching_hull_label"
        return self.structure.veto(self.side, self.close) or "ready"

    @property
    def entry_side(self) -> int:
        # `side` intentionally remains the pre-structure side used by existing exits.
        return self.side if self.entry_reason == "ready" else 0

    def entry_audit(self):
        band = self.structure.band(self.label)
        return {
            "entry_policy": ENTRY_POLICY,
            "range_midpoint": self.structure.midpoint,
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
