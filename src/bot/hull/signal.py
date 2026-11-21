"""The pasted FC DynHull on native chart candles, with the original execution stop.

`label` is the raw Pine BUY/SELL, even if no valid stop or entry exists. A projection
is never appended to closed indicator/structure/stop history.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace

from bot.zones.engine import Bar

from .config import ENTRY_POLICY, TF_NAME, Indicator
from .curves import WMA, Baseline, LiveTick
from .entry_clock import utc
from .structure import InternalStructure, Structure


@dataclass(frozen=True, slots=True)
class Reading:
    close_s: int
    tf: int
    close: float
    a: float | None
    b: float | None
    atr: float | None
    label: int
    side: int  # protective-stop-ready side; NEVER the exit signal
    reason: str
    anchor: float | None
    anchor_s: int | None
    raw_sl: float | None
    window_start_s: int | None
    prior_bars: int
    pivot_low: float | None
    pivot_high: float | None
    structure: Structure = field(default_factory=Structure)
    active_source: str = "wait"
    a_low: float | None = None
    a_high: float | None = None
    cross_low: int = 0
    cross_high: int = 0
    trigger: LiveTick | None = None
    skip_after_mss: bool | None = None

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
    def candidate_reason(self):
        if not self.label:
            return "no_cross"
        if not self.structure.bias:
            return "neutral_structure"
        if self.label != self.structure.bias:
            return "opposite_signal_exit_only"
        if self.side != self.label or self.raw_sl is None:
            return self.reason if self.reason != "ready" else "no_valid_initial_stop"
        return "ready"

    @property
    def entry_reason(self):
        reason = self.candidate_reason
        if (
            reason == "ready"
            and self.structure.epoch is not None
            and self.skip_after_mss is not False
        ):
            return "first_trend_cross_after_mss"
        return reason

    @property
    def entry_side(self):
        return self.label if self.entry_reason == "ready" else 0

    def entry_audit(self):
        return {
            "entry_policy": ENTRY_POLICY,
            "structure_phase": self.structure.phase,
            "entry_setup": "trend_hull_cross" if self.entry_side else None,
            "entry_side": self.entry_side,
            "entry_reason": self.entry_reason,
        }


class FirstCrossGate:
    """One first trend-aligned signal per confirmed structure shift, not per tick/fill."""

    def __init__(self):
        self.bind({}, None)

    def bind(self, record, persist):
        self.record, self.persist = record, persist
        self.saved = record.get("epoch"), record.get("bar")

    def apply(self, r, *, observe):
        s = r.structure
        epoch, known = s.epoch, self.record.get("epoch")
        # After a restart, the shift can predate a finite history reload. Restore
        # only a matching *confirmed* context at/after its original timestamp.
        if (
            epoch is None
            and known is not None
            and s.closed_s is not None
            and s.closed_s >= known
            and s.bias == self.record.get("bias")
        ):
            epoch = known
            r = replace(r, structure=replace(s, last_mss_s=known))
        if epoch is None:
            return r
        newer = known is None or epoch > known
        older = known is not None and epoch < known
        bar = None if newer else self.record.get("bar")
        if newer and observe:
            self.record.update(
                epoch=epoch, bias=s.bias, bar=None
            )  # armed, not consumed
        if r.candidate_reason != "ready":
            return r
        # A reload cannot roll back live state. Same-bar rechecks never promote
        # the skipped signal. Actual order admission/fills are intentionally later.
        skip = older or bar is None or r.close_s <= bar
        if observe and not older and bar is None:
            self.record["bar"] = r.close_s
        return replace(r, skip_after_mss=skip)

    def flush(self):
        stamp = self.record.get("epoch"), self.record.get("bar")
        if self.persist is not None and stamp != self.saved:
            self.persist()
            self.saved = stamp


class Hull:
    def __init__(self, tf: int, p: Indicator, pip_size: float, gate=None):
        if tf not in TF_NAME:
            raise ValueError("Unsupported native Hull timeframe")
        self.tf, self.p, self.pip = tf, p, pip_size
        self.gate = gate if gate is not None else FirstCrossGate()
        n = p.hull_len
        h = max(1, n // 2)
        self.fixed = p.custom_source if p.source == "custom" else p.source
        sources = {"low", "high"} if p.source == "dynamic" else {self.fixed}
        self.baselines = {src: Baseline(h, n) for src in sources}
        self.b_weights = WMA(max(1, h // 3)), WMA(max(1, h // 2)), WMA(h), WMA(h)
        self.prev = self.last_b = self.atr = None
        self.n = 0
        self.tr_sum = 0.0
        self.hist = deque(maxlen=p.pullback_bars)
        self.structure = InternalStructure(p.swing_len)
        self.last_cross = {1: None, -1: None}

    def update(
        self, c: Bar, *, context: Structure | None = None, observe_first=True
    ) -> Reading:
        if self.prev and c.t <= self.prev.t:
            raise ValueError("Closed bars must be unique and ordered")
        return self._read(c, None, context, observe_first)

    def preview(
        self, c: Bar, tick: LiveTick, *, context: Structure | None = None
    ) -> Reading:
        period = self.tf * 60
        if (
            not self.prev
            or c.t != self.prev.t + period
            or c.c != tick.bid
            or not c.t * 1000 <= tick.ms < (c.t + period) * 1000
        ):
            raise ValueError("Live bid candle and tick must follow the closed state")
        return self._read(c, tick, context, False)

    def _read(self, c, tick, context, observe_first):
        commit, p = tick is None, self.p
        close_s = c.t + self.tf * 60
        native = self.structure.update(c, close_s) if commit else self.structure.view
        structure = native if context is None else context
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
        if p.source == "dynamic":
            active = (
                "low"
                if structure.bias == 1
                else "high"
                if structure.bias == -1 or p.neutral_mode == "high"
                else "wait"
            )
        else:
            active = self.fixed
        # The crossover returned by each Baseline uses THAT curve's own prior A.
        # Never OR the outputs and never compare High now to Low on the prior bar.
        a, label = curves.get(active, (None, 0))
        prior = list(self.hist) if label else []
        if commit:
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
        anchor = when = sl = window = None
        count = 0
        side = 0
        reason = "no_cross"
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
                chosen = self.structure.last_l if label == 1 else self.structure.last_h
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
            else:
                side = label
                reason = "ready"
            if commit:
                self.last_cross[label] = c.t
        if commit:
            self.prev, self.last_b = c, b
        r = Reading(
            close_s,
            self.tf,
            c.c,
            a,
            b,
            self.atr,
            label,
            side,
            reason,
            anchor,
            when,
            sl,
            window,
            count,
            self.structure.last_l.price if self.structure.last_l else None,
            self.structure.last_h.price if self.structure.last_h else None,
            structure,
            active,
            curves.get("low", (None, 0))[0],
            curves.get("high", (None, 0))[0],
            curves.get("low", (None, 0))[1],
            curves.get("high", (None, 0))[1],
            tick,
        )
        return self.gate.apply(r, observe=observe_first)
