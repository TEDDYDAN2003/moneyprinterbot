"""One native M1 clock per symbol. Parents supply closed structure, never trigger candles.

No M1 structure, projected entry, parent Hull entry, cross-source comparison, or
backfill. Quotes are verified against the immediate successor bid candle. One
arbitration result is irrevocable, even if the winner's broker checks veto it.
"""

from __future__ import annotations

from dataclasses import replace

from .curves import LiveTick
from .entry_clock import WAITING
from .feed import Stream, sample
from .orders import cross_candle_stop
from .signal import FirstCrossGate


def crosses(r, indicator):
    if indicator.source == "dynamic":
        return {"low": r.cross_low, "high": r.cross_high}
    return {r.active_source: r.label}


def current(view, tf, asof):
    return view.closed_s is not None and 0 <= asof - view.closed_s < tf * 60


def candidate(raw, candle, view, tf, minute, detected):
    """Attach parent context ex ante; never use the M1 engine's active bias/stop."""
    if raw.trigger is not None or raw.tf != 1 or candle.t + 60 != raw.close_s:
        raise ValueError("M1 candidate needs the just-completed native candle")
    if view.bias not in (-1, 1) or not current(view, tf, raw.close_s):
        return None
    p = minute.cfg
    if p.source == "dynamic":
        src = "low" if view.bias == 1 else "high"
        value = raw.a_low if view.bias == 1 else raw.a_high
    else:
        src, value = raw.active_source, raw.a
    sign = crosses(raw, p)[src]
    if sign != view.bias:
        return None
    wick = candle.l if sign == 1 else candle.h
    pad = (
        None if raw.atr is None else p.atr_buffer * raw.atr + p.extra_pips * minute.pip
    )
    sl = (
        cross_candle_stop(wick, sign, pad, minute.tick_size)
        if pad is not None
        else None
    )
    ready = sl is not None and sl > 0 and sign * (candle.c - sl) > 0
    return replace(
        raw,
        tf=tf,
        trigger_tf=1,
        detected_s=detected,
        active_source=src,
        a=value,
        structure=view,
        label=sign,
        side=sign if ready else 0,
        reason="ready" if ready else "invalid_m1_cross_stop",
        anchor=wick,
        anchor_s=candle.t,
        raw_sl=sl,
        window_start_s=candle.t,
        stop_tick_size=minute.tick_size,
        pivot_low=None,
        pivot_high=None,
        skip_after_mss=None,
        waived_first_cross=False,
    )


def choose(rows):
    eligible = [r for r in rows if r.entry_side]
    if not eligible:
        return None, "no_eligible_m1_cross"
    if len({r.entry_side for r in eligible}) > 1:
        return None, "conflicting_m1_parents"
    return max(eligible, key=lambda r: r.tf), "selected"


class MinuteBook:
    def __init__(self, h, profile, tick_size, parents, records, persist):
        self.parents = parents
        self.m = Stream(
            1, h.indicator, profile.pip_size, tick_size=tick_size, price_only=True
        )
        self.gates = {tf: FirstCrossGate() for tf in parents}
        for tf, g in self.gates.items():
            g.bind(records.setdefault(str(tf), {}), persist)
        self.last_live = None
        self.live_signs = {}
        self.healthy = {}

    def _read_live(self, d, name, quote, clock):
        """One bid/ask/time snapshot on both sides of the native forming-bar read."""
        q = LiveTick.read(quote)
        now = clock()
        if not q or not 0 <= now - q.ms / 1000 <= d.lim.max_quote_age_s:
            return None
        r = self.m.project(d.mt, name, q)
        after = LiveTick.read(d.mt.symbol_info_tick(name))
        now = clock()
        if (
            r is None
            or r.trigger != q
            or now >= r.close_s
            or after != q
            or not 0 <= now - q.ms / 1000 <= d.lim.max_quote_age_s
            or (self.last_live and q.ms < self.last_live.ms)
        ):
            return None
        return r

    def sources(self, asof):
        # Only curves selected by actual, closed parent bias can broadcast a raw
        # exit to OTHER parent owners. A ticket always retains its own source too.
        if self.m.cfg.source != "dynamic":
            return {self.m.h.fixed}
        return {
            "low" if v.bias == 1 else "high"
            for tf, lane in self.parents.items()
            if self.healthy.get(tf)
            and (v := lane.h.structure.view).bias in (-1, 1)
            and current(v, tf, asof)
        }

    def _exits(self, d, name, r, clock):
        raw = crosses(r, self.m.cfg)
        signs = {src: sign for src, sign in raw.items() if sign}
        if r.trigger is not None:
            warm = (
                self.m.baseline_due
                or self.m.last_sample is None
                or not 0 <= clock() - self.m.last_sample <= d.lim.max_quote_age_s
            )
            prior = (
                self.live_signs
                if self.last_live
                and self.last_live.ms // 60000 == r.trigger.ms // 60000
                else {}
            )
            signs = (
                {}
                if warm
                else {
                    src: sign for src, sign in signs.items() if prior.get(src) != sign
                }
            )
            self.live_signs, self.last_live = raw, r.trigger
            self.m.baseline_due, self.m.last_sample = False, clock()
        if signs:
            d.minute_opposite(
                name,
                signs,
                r.signal_msc,
                live=r.trigger is not None,
                active_sources=self.sources(r.signal_s),
            )
        return raw

    def watch(self, d, name, clock):
        if self.healthy.get(1):
            live = self._read_live(d, name, d.mt.symbol_info_tick(name), clock)
            if live is not None:
                self._exits(d, name, live, clock)

    def verify(self, d, name, r, quote, clock):
        # Independently service M1 exits even if our parent context or entry is no longer valid.
        live = self._read_live(d, name, quote, clock)
        if live is None:
            return False
        signs = self._exits(d, name, live, clock)
        prev = self.m.h.prev
        lane = self.parents[r.tf]
        v = lane.h.structure.view
        # Observe the parent's original protective cross, not a parent entry gate.
        # Prime the lane's live baseline before the first ticket exists. A newly
        # observed parent cross against already-open legs interrupts the batch.
        parent = lane.project(d.mt, name, live.trigger)
        if LiveTick.read(d.mt.symbol_info_tick(name)) != live.trigger:
            return False
        if (
            parent is not None
            and parent.trigger == live.trigger
            and clock() < parent.close_s
        ):
            if lane.note(parent, baseline=lane.baseline_due) and d.close_opposite(
                name, parent
            ):
                return False
            lane.baseline_due = False
            lane.last_sample, lane.last_tick_ms = clock(), parent.trigger.ms
        return (
            r.trigger_tf == 1
            and r.trigger is None
            and prev is not None
            and prev.t == r.anchor_s == r.close_s - 60
            and live.close_s == r.close_s + 60
            and live.trigger.ms >= r.signal_msc
            and clock() < r.close_s + 60
            and r.anchor == (prev.l if r.side == 1 else prev.h)
            and r.stop_tick_size is not None
            and r.stop_tick_size == self.m.tick_size
            and current(v, r.tf, clock())
            and v.closed_s == r.structure.closed_s
            and v.epoch == r.structure.epoch
            and v.bias == r.side
            and all(
                signs.get(src) != -r.side
                for src in self.sources(live.signal_s) | {r.active_source}
            )
            and not d.minute_invalidated(name, r)
        )

    def _refresh(self, stream, d, name, clock):
        now = clock()
        prev = stream.h.prev
        # No closed history RPC is due between known native boundaries. On missing
        # data retry rather than caching a failure as usable structure.
        if (
            prev
            and self.healthy.get(stream.tf)
            and prev.t + stream.tf * 60 <= now < prev.t + 2 * stream.tf * 60
        ):
            return None, "unchanged"
        r, why = stream.refresh(d.mt, name, now)
        self.healthy[stream.tf] = (
            why != "history_unavailable" and stream.h.prev is not None
        )
        return r, why

    def poll(self, d, name, clock, *, history_ok, bid_chart):
        if not bid_chart:
            for key in list(d.waiting):
                if key[0] == name:
                    d.cancel_wait(*key, "unverified_bid_chart")
            return
        raw, why = self._refresh(self.m, d, name, clock)
        detected = clock()
        healthy = self.healthy.get(1) and why != "warmup_only"
        active = []
        for tf, lane in self.parents.items():
            parent, status = self._refresh(lane, d, name, clock)
            healthy = healthy and self.healthy.get(tf) and status != "warmup_only"
            if self.healthy.get(tf):
                if (name, tf) in d.open_lanes:
                    active.append(lane)
                if status != "unchanged":
                    for shift in lane.shifts:
                        d.close_mss(name, shift)
                if parent is not None and parent.structure.event_code:
                    v = parent.structure
                    d.emit(
                        "structure_break",
                        symbol=name,
                        tf=tf,
                        close_s=v.closed_s,
                        event_code=v.event_code,
                        bias=v.bias,
                        established_bias=v.established_bias,
                        failed_counter_mss_s=v.failed_counter_mss_s,
                        is_failed_mss_return=abs(v.event_code) == 2 and v.failed_return,
                    )
                if parent is not None and parent.label:
                    d.close_opposite(
                        name, parent
                    )  # original parent exits; never a parent entry
        if raw is not None:
            self._exits(d, name, raw, clock)
        self.watch(d, name, clock)
        # A bad parent projection may not swallow exits observed on other parents.
        if active:
            live_parents, _ = sample(d.mt, name, active, clock, d.lim.max_quote_age_s)
            for parent in live_parents:
                d.close_opposite(name, parent)
        waits = [key for key in d.waiting if key[0] == name]
        for key in waits:
            w = d.waiting[key]
            if (
                not healthy
                or not history_ok
                or (raw is not None and raw.close_s > w.reading.close_s)
            ):
                d.cancel_wait(*key, "m1_wait_superseded_or_data_unavailable")
            else:
                result = d.resume_first_quote(*key)
                if result != WAITING:
                    self.decision(d, name, w.reading, result)
        if raw is None or not any(crosses(raw, self.m.cfg).values()):
            return
        views = {tf: s.h.structure.view for tf, s in self.parents.items()}
        if not healthy or not all(
            current(v, tf, raw.close_s) for tf, v in views.items()
        ):
            d.emit(
                "m1_arbitration",
                symbol=name,
                close_s=raw.close_s,
                outcome="m1_parent_or_history_unavailable",
            )
            return
        candidates = []
        for tf, v in views.items():
            r = candidate(raw, self.m.h.prev, v, tf, self.m, detected)
            if r is None:
                continue
            gate = self.gates[tf]
            r = gate.apply(r, observe=True)
            gate.flush()
            candidates.append(r)
        owner, outcome = choose(candidates)
        d.emit(
            "m1_arbitration",
            symbol=name,
            close_s=raw.close_s,
            outcome=outcome,
            m1_raw=crosses(raw, self.m.cfg),
            parent_bias={str(f): v.bias for f, v in views.items()},
            close_to_detect_ms=round((detected - raw.close_s) * 1000, 3),
            owner_tf=owner.tf if owner else None,
            candidates=[
                {"tf": r.tf, "side": r.label, "reason": r.entry_reason}
                for r in candidates
            ],
        )
        if owner is None:
            return
        d.emit(
            "closed_label",
            symbol=name,
            tf=owner.tf,
            close_s=owner.close_s,
            close=owner.close,
            label=owner.label,
            hull_a=owner.a,
            hull_b=owner.b,
            atr=owner.atr,
            stop_tick_size=owner.stop_tick_size,
            **owner.clock_fields(),
            **owner.entry_audit(),
        )
        result = (
            d.batch(name, owner, raw_tf=1)
            if history_ok
            else "data_or_deal_history_unavailable"
        )
        if result != WAITING:
            self.decision(d, name, owner, result)

    @staticmethod
    def decision(d, name, r, outcome):
        d.emit(
            "label_decision",
            symbol=name,
            tf=r.tf,
            raw_selected_tf=1,
            close_s=r.close_s,
            **r.clock_fields(),
            **r.entry_audit(),
            outcome=outcome,
            signal_session=d.session_gate(name).report(r.signal_s),
        )
