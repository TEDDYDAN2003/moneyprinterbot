"""Immutable evidence for a tick entry. A raw Hull.preview is still exit-only."""

from dataclasses import dataclass, fields
from math import isfinite

from .config import CROSS_REGION_POLICY, INTRA_ENTRY, INTRA_STOP
from .orders import cross_candle_stop
from .signal import Reading
from .structure import Pivot


@dataclass(frozen=True, slots=True)
class IntrabarReading(Reading):
    swing: Pivot | None = None
    intrabar = True

    @property
    def gate_stamp(self):
        return (
            self.signal_msc
        )  # fresh recross within the same candle is a distinct event

    @property
    def candidate_reason(self):
        q, pv, cv = self.trigger, self.structure, self.trigger_structure
        start = self.close_s - self.candle_tf * 60
        if (
            not self.paired
            or q is None
            or cv is None
            or cv.closed_s != start
            or not start * 1000 <= q.ms < self.close_s * 1000
            or pv.closed_s is None
            or not 0 <= q.ms - pv.closed_s * 1000 < self.tf * 60000
        ):
            return "intrabar_context_unverified"
        if self.label not in (-1, 1) or cv.bias != pv.bias or self.label != cv.bias:
            return "pair_structure_disagrees"
        if self.a is None or self.b is None or not self.label * (self.b - self.a) > 0:
            return "intrabar_cross_unverified"
        if self.cross_price is None:
            return "cross_region_unavailable"
        if self.label * (q.bid - self.cross_price) <= 0:
            return "cross_region_wrong_side_or_too_close"
        pivot = self.swing
        if not (
            pivot
            and pivot == cv.range_swing
            and pivot.bar_s < pivot.known_s <= start
            and self.anchor == pivot.price
            and self.anchor_s == pivot.bar_s
            and self.label * (q.bid - pivot.price) > 0
        ):
            return "trigger_range_swing_unavailable"
        if not (
            self.side == self.label
            and self.raw_sl is not None
            and isfinite(self.raw_sl)
            and self.raw_sl > 0
            and self.stop_tick_size
            and self.stop_tick_size > 0
            and self.side * (pivot.price - self.raw_sl) > 0
        ):
            return "invalid_swing_stop"
        return "ready"

    def order_tag(self, leg):
        return f"q{self.tf}x{self.candle_tf}_{self.signal_msc}_{leg}"

    def clock_fields(self):
        return Reading.clock_fields(self) | {"entry_timing": "intrabar_cross"}

    def entry_audit(self):
        return {
            "entry_policy": INTRA_ENTRY,
            "initial_stop_policy": INTRA_STOP,
            "entry_setup": "intrabar_pair_swing" if self.entry_side else None,
            "entry_side": self.entry_side,
            "entry_reason": self.entry_reason,
            "parent_tf": self.tf,
            "trigger_tf": self.candle_tf,
            "trigger_source": self.active_source,
            "parent_structure_bias": self.structure.bias,
            "parent_structure_close_s": self.structure.closed_s,
            "trigger_structure_bias": self.trigger_structure.bias,
            "trigger_structure_close_s": self.trigger_structure.closed_s,
            "swing_kind": "HL" if self.label == 1 else "LH",
            "swing_price": self.anchor,
            "swing_bar_s": self.anchor_s,
            "swing_known_s": self.swing.known_s if self.swing else None,
            "frozen_swing_sl": self.raw_sl,
            "cross_region_policy": CROSS_REGION_POLICY,
            "hull_cross_price": self.cross_price,
            "cross_region_candle_close": None,
            "cross_candle_raw_sl": None,
            "trigger_bid": self.trigger.bid,
            "trigger_ask": self.trigger.ask,
            "origin_bar_end_s": self.close_s,
            "trigger_to_detect_ms": round((self.detected_s - self.signal_s) * 1000, 3),
            "failed_counter_mss_s": self.structure.failed_counter_mss_s,
            "return_mss_s": self.structure.epoch
            if self.structure.failed_return
            else None,
            "mss_skip_exception": "failed_counter_mss_return"
            if self.waived_first_cross and self.entry_side
            else None,
        }

    def guard(self):
        return {
            "policy": INTRA_ENTRY,
            "ms": self.signal_msc,
            "end": self.close_s,
            "side": self.side,
            "source": self.active_source,
            "status": "watch",
        }


def candidate(raw, parent, view, lane, now):
    """Stop uses the TRIGGER's previously confirmed range, not the projected candle's extreme."""
    cv = raw.structure
    swing = cv.range_swing
    side = raw.label
    tick, atr = lane.tick_size, raw.atr
    stop = None
    if swing is not None and side in (-1, 1) and atr is not None:
        pad = lane.cfg.atr_buffer * atr + lane.cfg.extra_pips * lane.pip
        stop = cross_candle_stop(swing.price, side, pad, tick)
    data = {f.name: getattr(raw, f.name) for f in fields(Reading)}
    data.update(
        tf=parent,
        trigger_tf=raw.tf,
        structure=view,
        trigger_structure=cv,
        detected_s=now,
        skip_after_mss=None,
        waived_first_cross=False,
        side=side if stop is not None else 0,
        raw_sl=stop,
        anchor=swing.price if swing else None,
        anchor_s=swing.bar_s if swing else None,
        stop_tick_size=tick,
        swing=swing,
    )
    return IntrabarReading(**data)
