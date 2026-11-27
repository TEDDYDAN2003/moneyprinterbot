"""Closed native trigger + closed native parent, evaluated at the trigger's close.

M5/M10 can each be a parent AND a trigger. Read each stream once: roles have
separate entry gates and ticket ownership, not separate copies of the market.
"""

from __future__ import annotations

from dataclasses import replace

from .config import PAIRS, TF_NAME
from .curves import LiveTick
from .entry_clock import CLOCK_SKEW_TOLERANCE_S, WAITING
from .notifier import notify_blocked_setup
from .signal import FirstCrossGate


def asof(view, tf, instant):
    return view.closed_s is not None and 0 <= instant - view.closed_s < tf * 60


def pair_reading(raw, parent, view, detected, *, pairs=PAIRS):
    """Keep the *child's* actual label, ATR, wick and frozen stop. A parent is not a trigger."""
    if (
        raw.trigger is not None
        or raw.trigger_tf is not None
        or raw.tf != pairs.get(parent)
        or raw.structure.closed_s != raw.close_s
        or not asof(view, parent, raw.close_s)
    ):
        raise ValueError("Missing same-close trigger or as-of parent evidence")
    return replace(
        raw,
        tf=parent,
        trigger_tf=raw.tf,
        structure=view,
        trigger_structure=raw.structure,
        detected_s=detected,
        skip_after_mss=None,
        waived_first_cross=False,
    )


def select(rows):
    valid = [r for r in rows if r.entry_side]
    if not valid:
        reasons = {r.entry_reason for r in rows}
        if "cross_region_close_invalidated" in reasons:
            return None, "cross_region_close_invalidated"
        if "cross_region_unavailable" in reasons:
            return None, "cross_region_unavailable"
        return None, "no_aligned_eligible_pair"
    if len({r.entry_side for r in valid}) > 1:
        return None, "conflicting_simultaneous_pairs"
    return max(valid, key=lambda r: r.tf), "selected"


class PairBook:
    def __init__(self, parents, streams, records, *, pairs=PAIRS, skip_first=True):
        self.pairs = {p: pairs[p] for p in parents}
        self.by_child = {c: p for p, c in self.pairs.items()}
        self.streams = streams
        if set(streams) != set(self.pairs) | set(self.by_child):
            raise ValueError("Paired mode requires exactly its selected native clocks")
        self.gates = {p: FirstCrossGate(skip_first=skip_first) for p in parents}
        for p, g in self.gates.items():
            g.bind(records.setdefault(f"{p}:{self.pairs[p]}", {}), None)
        self.healthy = {}
        self.latest = {}
        self.pending = set()
        self.wait_notice = {}
        self.live_edges = {}
        self.last_raw = {}  # coherent native forming candles for a paired market verification

    def _refresh(self, d, name, tf, lane, clock):
        now = clock()
        prev = lane.h.prev
        if (
            self.healthy.get(tf)
            and prev
            and prev.t + tf * 60 <= now < prev.t + 2 * tf * 60
        ):
            return None
        r, status = lane.refresh(d.mt, name, now)
        self.healthy[tf] = status != "history_unavailable" and lane.h.n >= lane.min_bars
        if status in ("history_unavailable", "warmup_only"):
            self.latest.pop(tf, None)
        if tf in self.pairs and self.healthy[tf]:
            for shift in lane.shifts:
                d.close_mss(name, shift)
        if r is not None:
            self.latest[tf] = (r, clock())
            if r.structure.event_code:
                v = r.structure
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
            if r.label and tf in self.by_child:
                self.pending.add(r.close_s)
        return r

    def _protect(self, d, name, r):
        if not r.label:
            return 0
        n = 0
        if r.tf in self.by_child:
            n += d.pair_opposite(name, self.by_child[r.tf], r)
        if r.tf in self.pairs:
            n += d.close_opposite(
                name, r
            )  # parent protection is separate from the scoped child exit
        return n

    def _prefix_reads(self, reads, q, nxt):
        """Prove the OLDER tick before following a newer one. No mixed-bar proof.

        Same native candle, unchanged OHLC prefix extended by this observed Bid.
        For an already verified duplicate only, MT5 can already expose the next
        Bid's extreme: reuse the cached old projection, not the newer wick. Any
        unexplained wick, missing peer, ambiguous stamp or roll is a real gap.
        """
        if nxt is None or nxt.ms <= q.ms:
            return None
        proven = []
        for lane, raw in reads:
            prev = self.last_raw.get(lane.tf)
            if not prev or not raw or raw.trigger != q or not prev.forming_bar:
                return None
            old = prev.forming_bar
            if q.ms < prev.trigger.ms or (
                q.ms == prev.trigger.ms and q != prev.trigger
            ):
                return None
            prefix = replace(old, h=max(old.h, q.bid), l=min(old.l, q.bid), c=q.bid)
            if raw.forming_bar != prefix:
                if (
                    q != prev.trigger
                    or raw.active_source != prev.active_source
                    or raw.structure != prev.structure
                    or raw.forming_bar
                    != replace(
                        prefix, h=max(prefix.h, nxt.bid), l=min(prefix.l, nxt.bid)
                    )
                ):
                    return None
                raw = prev
            proven.append((lane, raw))
        return proven

    def _live(self, d, name, lanes, quote, clock, *, stable=False):
        """Bracket all projections with the same executable tick. Observe transitions,
        not just the first time a side appeared in a candle. Neither a bad sibling
        nor a disappearing live cross may rewrite the closed market history.
        """
        q = LiveTick.read(quote)
        if not q or not -CLOCK_SKEW_TOLERANCE_S <= clock() - q.ms / 1000 <= d.lim.max_quote_age_s:
            return {}, 0, None
        reads = [(lane, lane.project(d.mt, name, q)) for lane in lanes]
        advanced = None
        if stable:
            latest = d.mt.symbol_info_tick(name)
            nxt = LiveTick.read(latest)
            if q != nxt:
                reads = self._prefix_reads(reads, q, nxt)
                if reads is None:
                    return {}, 0, None
                advanced = latest
        now = clock()
        if not -CLOCK_SKEW_TOLERANCE_S <= now - q.ms / 1000 <= d.lim.max_quote_age_s:
            return {}, 0, None
        verified = {}
        actions = []
        for lane, r in reads:
            if r is None or r.trigger != q or now >= r.close_s:
                continue
            if lane.last_tick_ms is not None and q.ms < lane.last_tick_ms:
                lane.baseline_due = True
                continue
            warm = (
                lane.baseline_due
                or lane.last_sample is None
                or not 0 <= now - lane.last_sample <= d.lim.max_quote_age_s
            )
            edge = (r.close_s, r.active_source, r.label)
            if not warm and r.label and self.live_edges.get(r.tf) != edge:
                actions.append(r)
            self.live_edges[r.tf] = edge
            lane.last_sample, lane.last_tick_ms, lane.baseline_due = now, q.ms, False
            verified[r.tf] = r
            if stable:
                self.last_raw[r.tf] = r
        return verified, sum(self._protect(d, name, r) for r in actions), advanced

    def watch(self, d, name, clock, *, all_triggers=False):
        active = {p for sym, p in d.open_lanes if sym == name and p in self.pairs}
        tfs = active | {self.pairs[p] for p in active}
        if all_triggers:
            tfs.update(self.by_child)
        lanes = [self.streams[tf] for tf in sorted(tfs) if self.healthy.get(tf)]
        if lanes:
            self._live(d, name, lanes, d.mt.symbol_info_tick(name), clock)

    def verify(self, d, name, r, quote, clock):
        if (
            self.pairs.get(r.tf) != r.trigger_tf
            or not r.entry_side
            or r.trigger is not None
        ):
            return False
        child, parent = self.streams[r.trigger_tf], self.streams[r.tf]
        if d.retest is None:
            # A paired market reprice cannot use the older missing-peer waiver.
            while True:
                live, matched, advanced = self._live(
                    d, name, (child, parent), quote, clock, stable=True
                )
                if (
                    parent.tf not in live
                    or child.tf not in live
                    or r.cross_price is None
                    or r.side * (live[child.tf].trigger.bid - r.cross_price) <= 0
                    or not self._closed_proof(
                        d, name, r, child, parent, live, matched, clock()
                    )
                ):
                    return False
                if advanced is None:
                    return quote
                if clock() > r.signal_s + d.lim.max_entry_age_s:
                    return False
                quote = advanced
        live, matched, _ = self._live(d, name, (child, parent), quote, clock)
        if not live:
            # A newer tick may have arrived mid-projection; retry once with latest tick.
            fresh_q = d.mt.symbol_info_tick(name)
            if fresh_q is not None and LiveTick.read(fresh_q) != LiveTick.read(quote):
                live, matched, _ = self._live(d, name, (child, parent), fresh_q, clock)
        return self._closed_proof(d, name, r, child, parent, live, matched, clock())

    def _closed_proof(self, d, name, r, child, parent, live, matched, now):
        """Verify the immutable confirmed trigger against native bars on each leg."""
        cr = live.get(child.tf)
        item = self.latest.get(r.candle_tf)
        if not item:
            return False
        raw, _ = item
        if (
            raw.close_s,
            raw.label,
            raw.active_source,
            raw.raw_sl,
            raw.atr,
            raw.cross_price,
            raw.cross_fraction,
        ) != (
            r.close_s,
            r.side,
            r.active_source,
            r.raw_sl,
            r.atr,
            r.cross_price,
            r.cross_fraction,
        ):
            return False
        c = child.h.prev
        cv = child.h.structure.view
        pv = parent.h.structure.view
        base_ok = (
            not matched
            and c is not None
            and (r.cross_bar is None or r.cross_bar == c)
            and c.t == r.anchor_s == r.close_s - r.candle_tf * 60
            and r.anchor == (c.l if r.side == 1 else c.h)
            and r.stop_tick_size == child.tick_size
            and r.stop_tick_size is not None
            and asof(cv, r.candle_tf, now)
            and asof(pv, r.tf, now)
            and cv == r.trigger_structure
            and pv.closed_s == r.structure.closed_s
            and pv.epoch == r.structure.epoch
            and cv.bias == pv.bias == r.side
            and self.healthy.get(r.tf)
            and self.healthy.get(r.candle_tf)
            and d.pair_fence(name, r.tf, r.candle_tf, -r.side) < r.signal_msc
        )
        if not base_ok:
            return False
        if cr is None:
            # Forming bar projection was unavailable at this microsecond;
            # closed signal and structure bias are verified intact.
            return True
        q = cr.trigger
        return (
            cr.tf == r.trigger_tf
            and cr.close_s == r.close_s + r.candle_tf * 60
            and now < cr.close_s
            and q.ms >= r.signal_msc
            and (child.last_tick_ms is None or q.ms >= child.last_tick_ms)
            and cr.label != -r.side
            and cr.active_source == r.active_source
        )

    def _due(self, tf, stamp):
        # Native MT5 clocks, NOT subdivisions of a parent bar. E.g. M4 can span an M5 close.
        last = self.streams[tf].h.prev
        anchor = last.t if last is not None else 0
        return (stamp - anchor) % (tf * 60) == 0

    def _decision(self, d, name, r, outcome):
        d.emit(
            "label_decision",
            symbol=name,
            tf=r.tf,
            raw_selected_tf=r.candle_tf,
            close_s=r.close_s,
            **r.clock_fields(),
            **r.entry_audit(),
            outcome=outcome,
            signal_session=d.session_gate(name).report(r.signal_s),
        )
        if r.entry_side:
            notify_blocked_setup(
                symbol=name,
                tf=TF_NAME.get(r.tf, r.tf),
                trigger_tf=TF_NAME.get(r.candle_tf, r.candle_tf) if r.candle_tf else None,
                side=r.entry_side,
                reason=outcome,
            )

    def _candidates(self, stamp):
        """Only a healthy, still-possible peer can delay a same-close decision."""
        missing = set()
        prepared = []
        unavailable = {}
        for parent, child in self.pairs.items():
            if not self._due(child, stamp):
                continue
            lane, pl = self.streams[child], self.streams[parent]
            if not self.healthy.get(parent) or pl.history_fence >= stamp:
                unavailable[parent] = "parent_unavailable_or_warming"
                continue
            if not self.healthy.get(child) or lane.history_fence >= stamp:
                unavailable[parent] = "trigger_unavailable_or_warming"
                continue
            view = pl.h.structure.view
            current = asof(view, parent, stamp)
            parent_due = (
                view.closed_s is not None and view.closed_s + parent * 60 == stamp
            )
            if not current and not parent_due:
                unavailable[parent] = "parent_not_current_at_trigger"
                continue
            item = self.latest.get(child)
            if not item or item[0].close_s != stamp:
                if (
                    (not current or view.bias)
                    and lane.h.prev
                    and lane.h.prev.t + 2 * child * 60 == stamp
                ):
                    # Wait for exactly one unpublished close. Never wait for a
                    # trigger that has a known neutral, stale or unavailable parent.
                    missing.add(child)
                else:
                    unavailable[parent] = "no_eligible_trigger_at_close"
                continue
            raw, detected = item
            if not raw.label:
                continue
            if not current:
                if raw.side == raw.label == raw.structure.bias:
                    missing.add(parent)
                continue
            prepared.append(pair_reading(raw, parent, view, detected, pairs=self.pairs))
        return prepared, tuple(sorted(missing)), unavailable

    def _resolve(self, d, name, stamp, clock, history_ok):
        """Co-due history can publish over multiple polls; never extend admission."""
        consumed = d.state.setdefault("paired_arbitration_through", {})
        if consumed.get(name, -1) >= stamp:
            return True
        prepared, waiting, unavailable = self._candidates(stamp)
        expired = not 0 <= clock() - stamp <= d.lim.max_entry_age_s
        if waiting and not expired and history_ok:
            if self.wait_notice.get(stamp) != waiting:
                self.wait_notice[stamp] = waiting
                d.emit(
                    "pair_close_wait",
                    symbol=name,
                    close_s=stamp,
                    awaiting_tf=list(waiting),
                )
            return False
        # The parent skip is spent by a confirmed technical candidate, not by a
        # broker send. A separate timing/session/funding/history veto is not a refund.
        rows = [self.gates[r.tf].apply(r, observe=True) for r in prepared]
        owner, outcome = select(rows)
        if expired:
            owner, outcome = None, "entry_window_expired"
        elif not history_ok:
            owner, outcome = None, "data_or_deal_history_unavailable"
        consumed[name] = stamp
        d.persist()  # skip reservations and group decision together, before touching order APIs
        d.emit(
            "pair_arbitration",
            symbol=name,
            close_s=stamp,
            outcome=outcome,
            owner_tf=owner.tf if owner else None,
            trigger_tf=owner.candle_tf if owner else None,
            unavailable_pairs=unavailable,
            candidates=[
                {
                    "parent_tf": r.tf,
                    "trigger_tf": r.candle_tf,
                    "side": r.label,
                    "parent_bias": r.structure.bias,
                    "trigger_bias": r.trigger_structure.bias,
                    "entry_side": r.entry_side,
                    "close_to_detect_ms": round((r.detected_s - stamp) * 1000, 3),
                    "parent_close_s": r.structure.closed_s,
                    "trigger_close_s": r.close_s,
                    "cross_price": r.cross_price,
                    "cross_fraction": r.cross_fraction,
                    "signal_candle_close": r.close,
                    "reason": r.entry_reason,
                }
                for r in rows
            ],
        )
        if owner is not None:
            d.emit(
                "closed_label",
                symbol=name,
                tf=owner.tf,
                close_s=stamp,
                close=owner.close,
                label=owner.label,
                hull_a=owner.a,
                hull_b=owner.b,
                atr=owner.atr,
                stop_tick_size=owner.stop_tick_size,
                **owner.clock_fields(),
                **owner.entry_audit(),
            )
            for sym, tf in list(d.waiting):
                if sym == name:
                    d.cancel_wait(sym, tf, "superseded_by_new_pair_trigger")
            result = d.batch(name, owner, raw_tf=owner.candle_tf)
            if result != WAITING:
                self._decision(d, name, owner, result)
        return True

    def poll(self, d, name, clock, *, history_ok, bid_chart):
        retest = getattr(d, "retest", None)
        if not bid_chart:
            if retest and name in retest.active:
                retest.invalidate(
                    retest.active[name], "unverified_bid_chart", flatten=True
                )
            self.pending.clear()
            self.wait_notice.clear()
            for sym, tf in list(d.waiting):
                if sym == name:
                    d.cancel_wait(sym, tf, "unverified_bid_chart")
            return
        closed = []
        for tf, lane in self.streams.items():
            r = self._refresh(d, name, tf, lane, clock)
            if r is not None:
                closed.append(r)
        if retest and name in retest.active:
            b = retest.active[name]
            relevant = (b["parent"], b["child"])
            now = clock()
            if not history_ok or any(
                not self.healthy.get(f)
                or not asof(self.streams[f].h.structure.view, f, now)
                or self.streams[f].h.structure.view.bias != b["side"]
                for f in relevant
            ):
                retest.invalidate(
                    b, "pending_pair_context_unavailable_or_changed", flatten=True
                )
        for r in closed:
            self._protect(d, name, r)
        self.watch(d, name, clock, all_triggers=True)
        self._advance_closed(d, name, clock, history_ok)

    def _advance_closed(self, d, name, clock, history_ok):
        # Resume an existing first-quote/flatness window only once per poll. A fresh close
        # is never a second chance for an older trigger close or a different pair.
        for key, w in list(d.waiting.items()):
            if key[0] != name:
                continue
            r = w.reading
            if (
                any(stamp > r.close_s for stamp in self.pending)
                or not history_ok
                or not self.healthy.get(r.tf)
                or not self.healthy.get(r.candle_tf)
            ):
                d.cancel_wait(*key, "pair_wait_superseded_or_data_unavailable")
            else:
                result = d.resume_first_quote(*key)
                if result != WAITING:
                    self._decision(d, name, r, result)
        for stamp in sorted(self.pending):
            if self._resolve(d, name, stamp, clock, history_ok):
                self.pending.discard(stamp)
                self.wait_notice.pop(stamp, None)
