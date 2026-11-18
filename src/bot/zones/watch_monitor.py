"""Per-symbol forward watch. No historical fills: plan/send only at a current quote."""

from __future__ import annotations

import time
from dataclasses import asdict

import numpy as np

from .orders import pnl
from .price_action import adverse, arm, cancel_at_close, protect, return_plan, rules_for
from .quote_path import path_cancel, quotes_to_snapshot


class SymbolWatch:
    def __init__(self, s, z):
        self.s = s
        self.pa = z.price_action
        self.r = rules_for(z.rules, self.pa.policy)
        self.w = None
        self.cursor = 0
        self.last_wait = None
        self.paper = None

    def poll(
        self,
        desk,
        ctx,
        cold,
        discontinuity,
        bars_ok,
        own,
        armed,
        block,
        flatten,
        killed,
    ):
        from .monitor import fresh, save

        s = self.s
        pa = self.pa
        name = s.broker_symbol
        mt = desk.mt
        tick = mt.symbol_info_tick(name)
        now = time.time()
        ok = fresh(tick, now, desk.max_age)

        def emit(event, **kv):
            desk.emit(event, {"symbol": name, **kv})

        if ctx is not None:
            emit(
                "candle_decision",
                **asdict(ctx),
                status="startup_observe" if cold else "closed_bar",
            )
        # Broker SL/TP are always attached; sampled quote giveback is a software exit.
        if armed:
            mem = desk.state.setdefault("pa_management", {})
            current = {str(p.ticket) for p in own}
            changed = False
            for k, v in list(mem.items()):
                if v["symbol"] == name and k not in current:
                    del mem[k]
                    changed = True
            for p in own:
                if not ok or flatten or killed or p.sl <= 0 or p.tp <= 0:
                    continue
                side = 1 if p.type == mt.POSITION_TYPE_BUY else -1
                key = str(p.ticket)
                prior = mem.get(key, {})
                why = prior.get("exit_pending", "")
                peak = prior.get("peak", 0.0)
                if pa.policy.endswith("_managed"):
                    # Mark the configured leg on its executable side. Keep its peak across restarts.
                    q = tick.bid if side == 1 else tick.ask
                    marked = pnl(s, side, p.price_open, q - side * s.slippage)
                    peak, back = protect(peak, marked, s, pa)
                    opened = getattr(p, "time_msc", getattr(p, "time", 0) * 1000)
                    if (
                        not why
                        and ctx
                        and bars_ok
                        and not cold
                        and opened < ctx.t * 1000
                    ):
                        why = adverse(ctx, side, pa)
                    if not why and back:
                        why = "profit_giveback"
                    v = {"symbol": name, "peak": peak, "exit_pending": why}
                    if v != prior:
                        mem[key] = v
                        changed = True
                if why:
                    # Persist intent before attempting; failed closes retry while ticket is open.
                    save(desk.path, desk.state)
                    desk.close(p, why)
            if changed:
                save(desk.path, desk.state)
        elif self.paper is not None:
            d, p, cursor, old_bid, old_ask, seen_at_ms, peak = self.paper
            if not ok:
                return
            path = (
                quotes_to_snapshot(mt, name, cursor, tick)
                if int(tick.time_msc) - cursor <= 600_000
                else None
            )
            event = None
            exitmark = None
            if path is None:
                emit("shadow_censored", signal=d.t, reason="unverified_quote_history")
                self.paper = None
                return
            ms, b, a = path
            start = seen_at_ms
            if (
                start < 1
                or len(ms) < start
                or ms[0] != cursor
                or (ms[start - 1], b[start - 1], a[start - 1])
                != (cursor, old_bid, old_ask)
            ):
                emit(
                    "shadow_censored", signal=d.t, reason="quote_history_not_contiguous"
                )
                self.paper = None
                return
            q = (b if p.side == 1 else a)[start:]
            st = p.side * (q - p.sl) <= 0
            tp = p.side * (q - p.tp) >= 0
            ix = np.flatnonzero(st | tp)
            when = tick.time_msc
            if len(ix):
                i = ix[0]
                event = "stop" if st[i] else "target"
                exitmark = p.sl if st[i] else p.tp
                when = int(ms[start + i])
            else:
                exitmark = tick.bid if p.side == 1 else tick.ask
                marked = pnl(s, p.side, p.modeled_fill, exitmark - p.side * s.slippage)
                if killed or flatten:
                    event = "kill" if killed else "scheduled_break"
                elif pa.policy.endswith("_managed"):
                    peak, back = protect(peak, marked, s, pa)
                    if ctx and bars_ok:
                        event = adverse(ctx, p.side, pa) or None
                    if not event and back:
                        event = "profit_giveback"
            if event:
                emit(
                    "shadow_exit",
                    signal=d.t,
                    reason=event,
                    utc_ms=when,
                    net_usd_per_leg=pnl(
                        s, p.side, p.modeled_fill, exitmark - p.side * s.slippage
                    ),
                )
                self.paper = None
            else:
                self.paper = (
                    d,
                    p,
                    int(tick.time_msc),
                    tick.bid,
                    tick.ask,
                    int(np.sum(ms == tick.time_msc)),
                    peak,
                )
            return  # no immediate replacement of a shadow position in this poll

        def cancel(reason):
            if self.w:
                emit(
                    "watch_end", signal=self.w.d.t, zone=self.w.d.zone_id, reason=reason
                )
            self.w = None
            self.last_wait = None
            self.cursor = 0

        if cold or own or block or killed or not bars_ok or not ok:
            cancel("startup_or_busy_or_market_or_data_gate")
            return
        ms = int(tick.time_msc)
        if self.w:
            reason = (
                "data_gap"
                if discontinuity
                else "expired"
                if now * 1000 >= self.w.until_ms
                else cancel_at_close(self.w, ctx, ms)
                if ctx
                else ""
            )
            if reason:
                cancel(reason)
        if (
            self.w is None
            and ctx
            and ctx.side
            and ctx.t <= ms / 1000 <= now <= ctx.t + desk.lim.entry_age_seconds
        ):
            self.w, reason = arm(ctx, s, pa)
            emit(
                "watch_decision",
                signal=ctx.t,
                zone=ctx.zone_id,
                reason=reason,
                invalid_bid=self.w.invalid_bid if self.w else None,
            )
            if self.w:
                self.cursor = ctx.t * 1000
        if self.w is None:
            return
        path = quotes_to_snapshot(mt, name, self.cursor, tick)
        if path is None:
            cancel("unverified_quote_history")
            return
        if reason := path_cancel(self.w, path[1]):
            cancel(reason)
            return
        self.cursor = ms
        info = mt.symbol_info(name)
        if info is None:
            cancel("missing_contract")
            return
        p, reason = return_plan(
            self.w,
            s,
            self.r,
            tick.bid,
            tick.ask,
            ms,
            max(info.trade_stops_level, info.trade_freeze_level) * info.point,
        )
        if p is None:
            if self.last_wait != reason:
                emit("watch_wait", signal=self.w.d.t, reason=reason)
                self.last_wait = reason
            return
        d = self.w.d
        if armed:
            result = desk.send(s, d, watch=self.w)
        else:
            self.paper = (d, p, ms, tick.bid, tick.ask, int(np.sum(path[0] == ms)), 0.0)
            result = "shadow_open"
        emit(
            "return_decision",
            signal=d.t,
            zone=d.zone_id,
            quote_ms=ms,
            bid=tick.bid,
            ask=tick.ask,
            evidence=self.w.evidence,
            plan=asdict(p),
            outcome=result,
        )
        self.w = None
        self.last_wait = None
        self.cursor = 0
