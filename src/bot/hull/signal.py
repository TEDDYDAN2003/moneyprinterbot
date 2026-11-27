"""The pasted FC DynHull on native chart candles, with confirmed-cross execution.

`label` is the raw Pine BUY/SELL, even if no valid stop or entry exists. A projection
is never appended to closed indicator/structure/stop history.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from bot.hull.bar import Bar

from .config import (
    EXECUTION_INFO,
    M1_MODE,
    PAIR_MODE,
    PAIRS,
    CROSS_REGION_POLICY,
    TF_NAME,
    Indicator,
)
from .curves import WMA, Baseline, LiveTick
from .entry_clock import utc
from .orders import cross_candle_stop
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
    waived_first_cross: bool = False
    stop_tick_size: float | None = None  # verified trade grid at signal, not pip size

    trigger_tf: int | None = (
        None  # None = same-lane clock; otherwise tf owns a separate trigger_tf candle
    )
    detected_s: float | None = None
    trigger_structure: Structure | None = (
        None  # native, immutable close-time evidence for paired mode
    )

    cross_bar: Bar | None = None  # immutable trigger OHLC; never a forming candle
    cross_price: float | None = None  # selected Hull A/B line intersection
    cross_fraction: float | None = None  # fraction along the two crossing segments

    # The exact partial Bid candle at the observation, NEVER a finalized wick.
    forming_bar: Bar | None = None

    intrabar = (
        False  # only a fully attested IntrabarReading can take the tick-entry route
    )

    @property
    def gate_stamp(self):
        return self.close_s

    @property
    def paired(self):
        return self.trigger_tf is not None and (
            PAIRS.get(self.tf) == self.trigger_tf
            or {5: 4, 10: 8, 15: 12}.get(self.tf) == self.trigger_tf
        )

    @property
    def execution_mode(self):
        if self.trigger_tf is None:
            return "parent_close"
        if self.trigger_tf == 1:
            return M1_MODE
        return PAIR_MODE if self.paired else None

    def order_tag(self, leg):
        prefix = (
            f"p{self.tf}x{self.candle_tf}"
            if self.paired
            else f"{'m' if self.trigger_tf == 1 else 'h'}{self.tf}"
        )
        return f"{prefix}_{self.close_s}_{leg}"

    @property
    def candle_tf(self):
        return self.trigger_tf or self.tf

    @property
    def confirmed_mss(self):
        s = self.structure
        return (
            self.trigger_tf is None
            and self.trigger is None
            and s.closed_s == self.close_s
            and s.bias in (-1, 1)
            and s.event_code == 2 * s.bias
        )

    @property
    def signal_msc(self):
        return self.trigger.ms if self.trigger else self.close_s * 1000

    @property
    def signal_s(self):
        return self.trigger.ms / 1000 if self.trigger else self.close_s

    def clock_fields(self):
        return {
            "entry_timing": "live_exit_only" if self.trigger else "confirmed_close",
            "trigger_msc": self.signal_msc,
            "trigger_utc": utc(self.signal_s),
            "bar_open_utc": utc(self.close_s - self.candle_tf * 60),
            "bar_end_utc": utc(self.close_s),
            "signal_close_utc": None if self.trigger else utc(self.close_s),
        }

    @property
    def candidate_reason(self):
        if not self.label:
            return "no_cross"
        if self.paired:
            child = self.trigger_structure
            parent = self.structure
            if (
                child is None
                or child.closed_s != self.close_s
                or parent.closed_s is None
                or not 0 <= self.close_s - parent.closed_s < self.tf * 60
            ):
                return "pair_context_unverified"
            if child.bias == 0 or parent.bias == 0:
                return "neutral_pair_structure"
            if child.bias != parent.bias or self.label != child.bias:
                return "pair_structure_disagrees"
        if not self.structure.bias:
            return "neutral_structure"
        if self.label != self.structure.bias:
            return "opposite_signal_exit_only"
        if self.trigger is not None:
            return "await_cross_candle_close"
        if self.paired and self.cross_bar is not None:
            if self.cross_price is None:
                return "cross_region_unavailable"
            if self.label * (self.close - self.cross_price) <= 0:
                return "cross_region_close_invalidated"
        if self.side != self.label or self.raw_sl is None:
            return self.reason if self.reason != "ready" else "no_valid_initial_stop"
        return "ready"

    @property
    def entry_reason(self):
        reason = self.candidate_reason
        if (
            reason == "ready"
            and self.structure.failed_return
            and self.signal_msc <= self.structure.epoch * 1000
        ):
            return "mss_close_exit_only"
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
        entry_policy, stop_policy, setup = EXECUTION_INFO.get(
            self.execution_mode, (None, None, None)
        )
        ctx = {}
        if self.trigger_tf is not None:
            ctx.update(
                parent_tf=self.tf,
                trigger_tf=self.trigger_tf,
                parent_structure_close_s=self.structure.closed_s,
                close_to_detect_ms=round((self.detected_s - self.close_s) * 1000, 3)
                if self.detected_s is not None
                else None,
            )
            if self.trigger_tf == 1:
                ctx["m1_source"] = self.active_source
            elif self.paired:
                child = self.trigger_structure
                ctx.update(
                    trigger_source=self.active_source,
                    parent_structure_bias=self.structure.bias,
                    trigger_structure_bias=child.bias if child else None,
                    trigger_structure_close_s=child.closed_s if child else None,
                )
        return {
            **ctx,
            "entry_policy": entry_policy,
            "initial_stop_policy": stop_policy
            if self.stop_tick_size is not None
            else None,
            "cross_candle_open_s": self.anchor_s
            if self.stop_tick_size is not None
            else None,
            "cross_candle_wick": self.anchor
            if self.stop_tick_size is not None
            else None,
            "frozen_cross_sl": self.raw_sl if self.stop_tick_size is not None else None,
            "cross_region_policy": CROSS_REGION_POLICY if self.paired else None,
            "hull_cross_price": self.cross_price if self.paired else None,
            "cross_region_candle_close": self.close if self.paired and not self.trigger else None,
            "cross_candle_raw_sl": self.raw_sl if self.paired and not self.trigger else None,
            "structure_phase": self.structure.phase,
            "entry_setup": setup if self.entry_side else None,
            "entry_side": self.entry_side,
            "entry_reason": self.entry_reason,
            "mss_skip_exception": "failed_counter_mss_return"
            if self.waived_first_cross and self.entry_side
            else None,
            "failed_counter_mss_s": self.structure.failed_counter_mss_s,
            "return_mss_s": self.structure.epoch
            if self.structure.failed_return
            else None,
        }


class FirstCrossGate:
    """Reserve each epoch's first candidate once. Only a proven failed counter-MSS
    waives its ordinary veto. Structure, eligibility and actual orders are separate.
    """

    def __init__(self, *, skip_first: bool = True):
        self.skip_first = skip_first
        self.bind({}, None)

    def bind(self, record, persist):
        self.record, self.persist = record, persist
        self.saved = self._stamp()

    def _stamp(self):
        return tuple(self.record.get(k) for k in ("epoch", "bar", "waived_from"))

    def apply(self, r, *, observe):
        if r.trigger is not None and not r.intrabar:
            return r  # raw exits only; never arm, waive or spend a confirmed-entry reservation
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
            # Recover a skip watermark, never invent a failed-leg proof from a
            # matching direction alone when replay cannot establish the sequence.
            s = replace(s, last_mss_s=known)
            r = replace(r, structure=s)
        if epoch is None:
            return r
        newer = known is None or epoch > known
        older = known is not None and epoch < known
        bar = None if newer else self.record.get("bar")
        if newer and observe:
            self.record.update(
                epoch=epoch, bias=s.bias, bar=None
            )  # pending; the structural event itself is not an entry
            self.record.pop("waived_from", None)
        if r.candidate_reason != "ready":
            return r
        if s.failed_return and r.signal_msc <= epoch * 1000:
            return replace(r, skip_after_mss=True, waived_first_cross=False)
        # Keep v2 reservations: a previously skipped candidate may NOT turn
        # into a trade on another tick, on its close or during a history reload.
        # The journal marker permits re-verification of the same waived signal,
        # never a waiver without matching confirmed structural evidence.
        first = bar is None or r.gate_stamp == bar
        waive = (
            not older
            and first
            and s.failed_return
            and s.closed_s * 1000 <= r.signal_msc  # no later structure as evidence
            and (newer or self.record.get("bias") == s.bias)
            and (
                bar is None or self.record.get("waived_from") == s.failed_counter_mss_s
            )
        )
        skip = older or (self.skip_first and first and not waive)
        if observe and not older and bar is None:
            self.record["bar"] = r.gate_stamp
            if waive:
                self.record["waived_from"] = s.failed_counter_mss_s
        return replace(r, skip_after_mss=skip, waived_first_cross=waive)

    def flush(self):
        stamp = self._stamp()
        if self.persist is not None and stamp != self.saved:
            self.persist()
            self.saved = stamp


class Hull:
    def __init__(
        self,
        tf: int,
        p: Indicator,
        pip_size: float,
        gate=None,
        *,
        tick_size=None,
        price_only=False,
    ):
        if tf not in TF_NAME:
            raise ValueError("Unsupported Hull timeframe")
        self.tf, self.p, self.pip, self.tick_size = tf, p, pip_size, tick_size
        self.price_only = price_only
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
        self.structure = InternalStructure(p.swing_len)

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
        native = (
            self.structure.update(c, close_s)
            if commit and not self.price_only
            else self.structure.view
        )
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
        prior_b = self.last_b
        prior_a = {src: curve.last for src, curve in self.baselines.items()}
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
        cross_price = cross_fraction = None
        previous_a = prior_a.get(active)
        if label and None not in (previous_a, prior_b, a, b):
            before = prior_b - previous_a
            after = b - a
            denominator = after - before
            if (
                math.isfinite(before)
                and math.isfinite(after)
                and denominator != 0
                and before * after < 0
            ):
                fraction = -before / denominator
                if math.isfinite(fraction) and 0 < fraction < 1:
                    cross_fraction = fraction
                    cross_price = (
                        previous_a + fraction * (a - previous_a)
                        + prior_b + fraction * (b - prior_b)
                    ) / 2
                    if not math.isfinite(cross_price) or cross_price <= 0:
                        cross_price = cross_fraction = None
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
        anchor = when = sl = window = None
        side = 0
        reason = "no_cross"
        if label and commit:
            # The completed lane candle is the only stop anchor (including all three M15s of M45).
            anchor, when = (c.l if label == 1 else c.h), c.t
            window = c.t
            if self.atr is not None:
                pad = p.atr_buffer * self.atr + p.extra_pips * self.pip
                sl = cross_candle_stop(anchor, label, pad, self.tick_size)
            if sl is None:
                reason = "cross_stop_data_unavailable"
            elif sl <= 0 or label * (c.c - sl) <= 0:
                reason = "initial_stop_invalid_at_close"
            else:
                side = label
                reason = "ready"
        elif label:
            reason = "await_cross_candle_close"
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
            0,  # prior_bars in historical log schema
            self.structure.last_l.price if self.structure.last_l else None,
            self.structure.last_h.price if self.structure.last_h else None,
            structure,
            active,
            curves.get("low", (None, 0))[0],
            curves.get("high", (None, 0))[0],
            curves.get("low", (None, 0))[1],
            curves.get("high", (None, 0))[1],
            tick,
            stop_tick_size=self.tick_size if anchor is not None else None,
            cross_bar=c if anchor is not None else None,
            cross_price=cross_price,
            cross_fraction=cross_fraction,
            forming_bar=c if tick is not None else None,
        )
        return r if self.price_only else self.gate.apply(r, observe=observe_first)
