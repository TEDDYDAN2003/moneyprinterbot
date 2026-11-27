"""Paired market-tick clock. Closed streams supply structure, exits and finality, not entries."""

from .curves import LiveTick
from .intrabar_guard import relation
from .intrabar_signal import candidate
from .pairs import PairBook, asof, select


class IntrabarBook(PairBook):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.samples = {}  # last verified (quote, candle end, selected source, raw label)
        self.episodes = {}  # first observed tick of each uninterrupted nonzero edge
        self.repair_after = {}

    @staticmethod
    def _bid_chart(d, name):
        info = d.mt.symbol_info(name)
        return info is not None and getattr(info, "chart_mode", None) == getattr(
            d.mt, "SYMBOL_CHART_MODE_BID", 0
        )

    def _gap(self, lane):
        lane.baseline_due = True
        self.episodes.pop(lane.tf, None)

    def _sample(self, d, name, lanes, quote, clock):
        q = LiveTick.read(quote)
        if (
            q
            and self._bid_chart(d, name)
            and 0 <= clock() - q.ms / 1000 <= d.lim.max_quote_age_s
        ):
            reads = [(lane, lane.project(d.mt, name, q)) for lane in lanes]
            stable = LiveTick.read(d.mt.symbol_info_tick(name)) == q
            now = clock()
        else:
            stable = False
        if not stable or not 0 <= now - q.ms / 1000 <= d.lim.max_quote_age_s:
            for lane in lanes:
                self._gap(lane)
            return {}, [], 0
        verified, edges, protective = {}, [], []
        matched = 0
        for lane, raw in reads:
            tf = lane.tf
            if (
                raw is None
                or raw.trigger != q
                or raw.tf != tf
                or raw.a is None
                or raw.b is None
                or not raw.close_s - tf * 60 <= q.ms / 1000 <= now < raw.close_s
                or (lane.last_tick_ms is not None and q.ms < lane.last_tick_ms)
            ):
                self._gap(lane)
                continue
            prev = self.samples.get(tf)
            warm = (
                lane.baseline_due
                or prev is None
                or lane.last_sample is None
                or not 0 <= now - lane.last_sample <= d.lim.max_quote_age_s
                or q.ms - prev[0].ms > d.lim.max_quote_age_s * 1000
            )
            key = (raw.close_s, raw.active_source, raw.label)
            changed = prev is None or prev[1:] != key
            # Distinct prices can share an MT5 millisecond. Never manufacture a
            # second entry ID from such an ambiguous tick, even if it changes side.
            fresh = (
                not warm
                and changed
                and q.ms > prev[0].ms
                and (prev[1] != raw.close_s or prev[2] == raw.active_source)
            )
            if warm or changed or (q.ms == prev[0].ms and q != prev[0]):
                self.episodes.pop(tf, None)
            if fresh and raw.label:
                self.episodes[tf] = q.ms
                if tf in self.by_child:
                    edges.append(raw)
            if raw.label and not warm and changed:
                protective.append(
                    raw
                )  # warm observations are not fresh independent opposites
            elif warm and raw.label:
                # A baseline label may predate a same-candle fill. Only recover
                # an opposite for origins known to predate this entire candle.
                matched += d.origins.baseline_opposite(name, self.by_child.get(tf), raw)
            self.samples[tf] = (q, *key)
            lane.last_sample, lane.last_tick_ms, lane.baseline_due = now, q.ms, False
            verified[tf] = raw
        for tf, r in verified.items():
            if tf in self.by_child:
                matched += d.origins.observe(name, self.by_child[tf], r)
        matched += sum(self._protect(d, name, r) for r in protective)
        return verified, edges, matched

    def watch(self, d, name, clock, *, all_triggers=False):
        # Protection while another symbol is being serviced. Never enter recursively.
        tfs = {
            f
            for sym, p in d.open_lanes
            if sym == name and p in self.pairs
            for f in (p, self.pairs[p])
        }
        lanes = [self.streams[f] for f in sorted(tfs) if self.healthy.get(f)]
        if lanes:
            self._sample(d, name, lanes, d.mt.symbol_info_tick(name), clock)

    def verify(self, d, name, r, quote, clock):
        if not r.intrabar or self.pairs.get(r.tf) != r.trigger_tf or not r.entry_side:
            return False
        child = self.streams[r.candle_tf]
        parent = self.streams[r.tf]
        live, _, matched = self._sample(d, name, (child, parent), quote, clock)
        cr = live.get(child.tf)
        now = clock()
        if (
            cr is None
            or parent.tf not in live
            or matched
            or self.episodes.get(child.tf) != r.signal_msc
        ):
            return False  # never substitute closed proof, a different tick or a recross
        pv = parent.h.structure.view
        if (
            not self.healthy.get(child.tf)
            or not self.healthy.get(parent.tf)
            or r.signal_msc <= 1000 * max(child.history_fence, parent.history_fence)
            or not asof(pv, r.tf, now)
            or cr.close_s != r.close_s
            or cr.active_source != r.active_source
            or cr.label != r.side
            or cr.structure != r.trigger_structure
            or pv.closed_s != r.structure.closed_s
            or pv.bias != r.side
            or child.tick_size != r.stop_tick_size
            or not r.signal_msc <= cr.trigger.ms < r.close_s * 1000
            or r.side * (cr.trigger.bid - r.swing.price) <= 0
            or d.state.get("intrabar_parent_opposite_ms", {})
            .get(name, {})
            .get(f"{r.tf}:{-r.side}", -1)
            >= r.signal_msc
            or d.pair_fence(name, r.tf, child.tf, -r.side) >= r.signal_msc
        ):
            return False
        # Rebuild against the exact frozen evidence and check the persisted MSS gate.
        rebuilt = candidate(cr, r.tf, pv, child, now)
        return (
            rebuilt.swing == r.swing
            and rebuilt.raw_sl == r.raw_sl
            and rebuilt.atr == r.atr
            and rebuilt.cross_price == r.cross_price
            and rebuilt.cross_fraction == r.cross_fraction
            and relation(cr, r.active_source, r.side) is True
            and self.gates[r.tf].apply(r, observe=False).entry_side == r.side
        )

    def poll(self, d, name, clock, *, history_ok, bid_chart):
        if not bid_chart or not self._bid_chart(d, name):
            for lane in self.streams.values():
                self._gap(lane)
            return
        # Capture the exact originating close, even inside catch-up history. Its
        # absence is unknown, NOT a negative label on some later candle.
        matched = 0
        closed = []
        for tf, lane in self.streams.items():
            parent = self.by_child.get(tf)
            lane.capture_ends = (
                {g["end"] for _, g in d.origins.watching(name, parent, tf)}
                if parent
                else set()
            )
            lane.captured = []
            r = self._refresh(d, name, tf, lane, clock)
            for final in lane.captured:
                matched += d.origins.observe(name, parent, final)
            watched = (
                {g["end"] for _, g in d.origins.watching(name, parent, tf)}
                if parent
                else set()
            )
            now = clock()
            for end in watched:
                key = (tf, end)
                if end > now or now < self.repair_after.get(key, 0):
                    continue
                exact = lane.original_close(d.mt, name, end)
                if exact is not None:
                    matched += d.origins.observe(name, parent, exact)
                if any(
                    g["end"] == end for _, g in d.origins.watching(name, parent, tf)
                ):
                    if key not in self.repair_after:
                        d.emit(
                            "origin_close_unverified",
                            symbol=name,
                            parent_tf=parent,
                            trigger_tf=tf,
                            origin_bar_end_s=end,
                        )
                    self.repair_after[key] = (
                        now + 5
                    )  # keep original stops and retry only the exact close
            self.repair_after = {
                k: v
                for k, v in self.repair_after.items()
                if k[0] != tf or k[1] in watched
            }
            if r is not None:
                closed.append(r)
        for r in closed:
            matched += self._protect(d, name, r)
        self.pending.clear()  # close-only entries are intentionally never dispatched by this book
        tfs = set(self.by_child) | {
            p for sym, p in d.open_lanes if sym == name and p in self.pairs
        }
        lanes = [self.streams[f] for f in sorted(tfs) if self.healthy.get(f)]
        live, edges, interrupted = self._sample(
            d, name, lanes, d.mt.symbol_info_tick(name), clock
        )
        if not edges:
            return
        stamp = edges[0].trigger.ms
        decided = d.state.setdefault("intrabar_arbitration_ms", {})
        if decided.get(name, -1) >= stamp:
            return
        rows = []
        now = clock()
        for raw in edges:
            p = self.by_child[raw.tf]
            pv = self.streams[p].h.structure.view
            if (
                not self.healthy.get(p)
                or not asof(pv, p, now)
                or raw.trigger.ms
                <= 1000
                * max(self.streams[p].history_fence, self.streams[raw.tf].history_fence)
            ):
                continue
            row = candidate(raw, p, pv, self.streams[raw.tf], now)
            rows.append(self.gates[p].apply(row, observe=True))
        owner, why = select(rows)
        # A competing, currently aligned/healthy pair whose sample failed is
        # ambiguous. Drop this event rather than later picking a stale winner.
        missing = [
            c
            for p, c in self.pairs.items()
            if self.healthy.get(p)
            and self.healthy.get(c)
            and self.streams[p].h.structure.view.bias != 0
            and self.streams[p].h.structure.view.bias
            == self.streams[c].h.structure.view.bias
            and c not in live
        ]
        if not history_ok or missing:
            owner, why = None, "intrabar_data_unverified"
        elif matched or interrupted:
            owner, why = None, "prior_batch_not_flat_at_cross"
        decided[name] = stamp
        d.persist()  # includes first-cross gate and arbitration, before any order API
        d.emit(
            "intrabar_arbitration",
            symbol=name,
            trigger_msc=stamp,
            outcome=why,
            owner_tf=owner.tf if owner else None,
            candidates=[
                {
                    "parent_tf": x.tf,
                    "trigger_tf": x.candle_tf,
                    "side": x.label,
                    "reason": x.entry_reason,
                }
                for x in rows
            ],
        )
        if owner:
            d.emit(
                "intrabar_pair_cross",
                symbol=name,
                tf=owner.tf,
                close_s=owner.close_s,
                **owner.clock_fields(),
                **owner.entry_audit(),
            )
            outcome = d.batch(name, owner, raw_tf=owner.candle_tf)
            self._decision(d, name, owner, outcome)
