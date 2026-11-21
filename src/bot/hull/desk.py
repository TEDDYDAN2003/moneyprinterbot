"""Bounded demo batches with durable no-retry intent and broker-side SL and TP on EVERY leg."""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from bot.config.entry_sessions import EntrySessionVeto, sessions_for_runner
from bot.execution.demo_runner import DemoSafetyError, _filling_mode, _side_is_allowed
from bot.zones.hours import Hours
from bot.zones.monitor import account_check, fresh, save, snapshot

from .config import COUNTS, FIXED, LADDER, TARGET_POLICY, TF_NAME
from .entry_clock import FAST_POLL_S, WAITING, TimingBlock, check_quote, utc
from .ladder import Protection, executable, inside
from .ladder_desk import TicketLadders, opened_ms
from .orders import (
    Block,
    Exposure,
    Grid,
    count_for,
    make_plan,
    room,
    target,
    valid_cash,
)
from .ownership import Owners
from .signal import Reading


def _finite_or_none(value):
    # An unavailable MT5 estimate must never become zero or NaN in JSON logs.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def resolve(mt, profiles, *, check_lots=True):
    infos = mt.symbols_get()
    if infos is None:
        raise DemoSafetyError("Cannot read broker symbol catalogue")
    names = {v.name for v in infos}
    resolved = {}
    for key, p in profiles.items():
        possibilities = {p.broker_symbol} if p.broker_symbol else {key, *p.aliases}
        found = sorted(names & possibilities)
        if len(found) != 1:
            raise DemoSafetyError(
                f"{key}: exact matches {found}; set hull.symbols.{key}.broker_symbol to one Market Watch name"
            )
        name = found[0]
        if name in resolved or not mt.symbol_select(name, True):
            raise DemoSafetyError(f"{key}: duplicate or unavailable instrument {name}")
        if check_lots:
            Grid.from_info(mt.symbol_info(name)).volume(p.lots)
        resolved[name] = (key, p)
    return resolved


def identity_check(mt, mode, armed, identity=None):
    a = account_check(mt, mode, armed, identity)
    if (
        a.trade_mode != mt.ACCOUNT_TRADE_MODE_DEMO
        or a.currency != "USD"
        or a.margin_mode != mt.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
    ):
        raise DemoSafetyError(
            "Use a dedicated USD hedging DEMO terminal (including for preflight)"
        )
    if _finite_or_none(getattr(a, "equity", None)) is None or a.equity <= 0:
        raise DemoSafetyError("Invalid account equity")
    return a


@dataclass(slots=True)
class FirstQuoteWindow:
    reading: Reading
    end_mono: float
    raw_tf: int | None
    polls: int = 0
    last_wait_reason: str | None = None


class Desk:
    def __init__(
        self, mt, cfg, h, symbols, identity, state, path, emit, armed, *, sessions=None
    ):
        self.mt = mt
        self.h = h
        self.lim = h.limits
        self.syms = symbols
        self.ident = identity
        self.state = state
        self.path = path
        self.emit = emit
        self.armed = armed
        self.mode = cfg["run"]["mode"]
        self.kill = Path(
            cfg.get("safety", {}).get("emergency_flatten_hotkey_file", "KILL")
        )
        self.sessions = sessions if sessions is not None else sessions_for_runner()
        self.hours = Hours(cfg)
        self.ladders = TicketLadders(self)
        self.last_service = time.monotonic()
        self.verify_signal = (
            None  # installed by the native scanner; mandatory for live orders
        )
        self.exits = self.state.setdefault("opposite_exits", {})
        self.owners = Owners(state)
        self.mss_book = state.setdefault("last_lane_mss_exit", {})
        self.waiting: dict[
            tuple[str, int], FirstQuoteWindow
        ] = {}  # never restored after restart

    def persist(self):
        if self.armed:
            save(self.path, self.state)

    def account(self):
        return identity_check(self.mt, self.mode, self.armed, self.ident)

    def owned(self, p):
        return p.symbol in self.syms and p.magic == self.h.magic

    def typ(self, d):
        return self.mt.ORDER_TYPE_BUY if d == 1 else self.mt.ORDER_TYPE_SELL

    def cash(self, name):
        return lambda side, lots, entry, px: self.mt.order_calc_profit(
            self.typ(side), name, lots, entry, px
        )

    def exposure(self, ps, os):
        if os:
            raise Block("pending_orders_present")
        risk = 0.0
        for p in ps:
            if not self.owned(p) or not (p.sl > 0 and p.tp > 0):
                raise Block("foreign_or_unprotected_exposure")
            side = 1 if p.type == self.mt.POSITION_TYPE_BUY else -1
            risk += max(
                0.0,
                -valid_cash(self.cash(p.symbol), side, p.volume, p.price_open, p.sl),
            )
        return Exposure(len(ps), sum(p.volume for p in ps), risk)

    def session_gate(self, name):
        return self.sessions.bind(name, self.syms[name][0])

    def _quote_clock(self, gate, window, q, stage):
        # Timestamp this actual check, never the start of the scanner iteration.
        d = window.reading
        check = check_quote(
            d.signal_s,
            time.time(),
            self.lim,
            getattr(q, "time_msc", None),
            getattr(q, "bid", None),
            getattr(q, "ask", None),
            monotonic_expired=time.monotonic() > window.end_mono,
            live_bar_end=d.close_s if d.trigger else None,
        )
        if check.reason != "ready":
            # Even a missing quote cannot park a signal across the closing edge.
            gate.require(stage, decision=check.checked)
            raise TimingBlock(check, stage)
        return check

    def _entry_quote(self, name, window, stage):
        d = window.reading
        gate = self.session_gate(name)
        q = self.mt.symbol_info_tick(name)
        check = self._quote_clock(gate, window, q, stage)
        if self.verify_signal is not None or d.trigger is not None:
            # Verification can service a *protective* raw opposite / MSS between
            # legs. Never discard a usable closing-edge tick before it does so.
            verified = self.verify_signal is not None and self.verify_signal(name, d, q)
            gate.require(stage, decision=check.checked, quote=check.tick_msc / 1000)
            if not verified:
                raise Block("live_cross_or_trend_no_longer_verified")
            check = self._quote_clock(gate, window, q, stage)
        gate.require(stage, decision=check.checked, quote=check.tick_msc / 1000)
        if stage == "first_plan":
            self.emit(
                "entry_quote_check",
                symbol=name,
                tf=d.tf,
                signal=d.signal_s,
                stage=stage,
                polls=window.polls,
                disposition="fresh_quote",
                previous_wait=window.last_wait_reason,
                **check.audit(d.tf),
            )
        return q, check.checked

    def _margin_check(
        self, name, d, account, stage, legs, projected, *, per_leg=None, report=True
    ):
        """Funding is mandatory even at a 0% discretionary equity reserve."""
        free = _finite_or_none(getattr(account, "margin_free", None))
        projected = _finite_or_none(projected)
        pct = self.lim.min_free_margin_pct
        required = account.equity * (pct / 100)
        if free is None or projected is None:
            why = "no_margin_estimate"
        elif free < 0 or projected < 0:
            why = "insufficient_free_margin"
        elif projected < required:
            why = "margin_floor"  # explicit positive reserve, not a broker rejection
        else:
            return ""
        if report:
            self.emit(
                "margin_veto",
                symbol=name,
                tf=d.tf,
                signal=d.signal_s,
                stage=stage,
                reason=why,
                legs_checked=legs,
                equity_usd=account.equity,
                free_before_usd=free,
                estimated_margin_per_leg_usd=per_leg,
                projected_free_usd=projected,
                reserve_pct=pct,
                required_free_usd=required,
            )
        return why

    def _check_flat_at_trigger(self, name, reading, now):
        """A broker exit BETWEEN trigger and polling must not resurrect a repeat.

        Snapshot checks cover any surviving leg. Check the bounded interval for a
        SELL exit of a long (or BUY exit of a short), including manual/SL/TP deals
        whose magic may differ. An eligible closed trend cross may follow the independent exit of an opposite position.
        """
        if not 0 <= now - reading.signal_s <= self.lim.max_entry_age_s:
            return  # the normal quote-clock check rejects expired/future signals
        mt = self.mt
        deals = mt.history_deals_get(
            datetime.fromtimestamp(reading.signal_msc // 1000, UTC),
            datetime.fromtimestamp(int(now) + 1, UTC),
        )
        if deals is None:
            raise Block("entry_deal_history_unavailable")
        closing_type = mt.DEAL_TYPE_SELL if reading.side == 1 else mt.DEAL_TYPE_BUY
        for deal in deals:
            if (
                deal.symbol != name
                or deal.entry
                not in (mt.DEAL_ENTRY_OUT, mt.DEAL_ENTRY_OUT_BY, mt.DEAL_ENTRY_INOUT)
                or deal.type != closing_type
            ):
                continue
            # MT5 can assign a manual or server exit a different magic. Resolve the
            # actual opening position ID. An unknown recent exit remains a safety veto.
            owner = self.owners.book.get(str(getattr(deal, "position_id", None)))
            if (
                owner
                and owner["symbol"] == name
                and owner["magic"] == self.h.magic
                and owner["tf"] != reading.tf
            ):
                continue
            ms = getattr(deal, "time_msc", None)
            if not isinstance(ms, int) or ms <= 0:
                raise Block("entry_deal_history_unavailable")
            if ms >= reading.signal_msc:
                raise Block("same_side_batch_at_signal_close")

    def prepare(self, name, d, window, stage, expected=None, remaining=None):
        if TF_NAME.get(d.tf) not in self.h.eligible_timeframes:
            raise Block("timeframe_not_eligible")
        if not d.entry_side:
            raise Block(d.entry_reason)
        if self.mss_book.get(name, {}).get(str(d.tf), -1) >= d.signal_s:
            raise Block("confirmed_mss_exit")
        if any(
            x["symbol"] == name and x.get("tf") in (None, d.tf)
            for x in self.exits.values()
        ):
            raise Block("opposite_exit_not_confirmed_flat")
        mt = self.mt
        p = self.syms[name][1]
        a = self.account()
        now = time.time()
        # signal_s = the observed live cross tick OR the confirmed candle close,
        # never the projected end of a forming candle. Independent of p.session.
        self.session_gate(name).require(stage, signal=d.signal_s)
        if p.session == "configured" and (why := self.hours.status(now)[0]):
            raise Block(why)
        if self.kill.exists():
            raise Block("kill")
        if self.state.get("manual_hold"):
            raise Block("manual_reconciliation_required")
        if self.ladders.needs_review():
            raise Block("ladder_reconciliation_required")
        ps, os = snapshot(mt)
        e = self.exposure(ps, os)
        here = [(r, self.owners.lane(r)) for r in ps if r.symbol == name]
        if any(tf is None for _, tf in here):
            raise Block("unattributed_hull_position_review")
        tickets = {r.ticket for r, tf in here if tf == d.tf}
        if tickets != (expected or set()):
            if stage == "first_plan" and any(
                tf == d.tf and r.type == self.typ(d.side) for r, tf in here
            ):
                raise Block("same_side_batch_open")
            raise Block("symbol_busy_or_batch_changed")
        if stage == "first_plan" or (stage == "tick_reprice" and not expected):
            self._check_flat_at_trigger(name, d, time.time())
        i = mt.symbol_info(name)
        grid = Grid.from_info(i)
        if not _side_is_allowed(mt, i, "BUY" if d.side == 1 else "SELL"):
            raise Block("broker_side_disabled")
        q, _ = self._entry_quote(name, window, stage)
        plan = make_plan(d, p, grid, q.bid, q.ask, self.cash(name))
        margin = _finite_or_none(
            mt.order_calc_margin(self.typ(d.side), name, p.lots, plan.entry)
        )
        free = _finite_or_none(getattr(a, "margin_free", None))
        min_legs = min(COUNTS[p.rate]) if remaining is None else remaining

        def funding_veto(n):
            projected = (
                free - n * margin
                if free is not None and margin is not None and margin >= 0
                else None
            )
            # Don't log a terminal veto if a smaller allowed fast/medium batch can still fit.
            return self._margin_check(
                name, d, a, stage, n, projected, per_leg=margin, report=n == min_legs
            )

        if remaining is None:
            try:
                n = count_for(p.rate, self.lim, e, plan, funding_veto)
            except Block as exc:
                if str(exc) == "portfolio_risk_limit":
                    self.emit(
                        "risk_veto",
                        symbol=name,
                        sl=plan.sl,
                        risk_per_leg=plan.risk_usd,
                        min_legs=min(COUNTS[p.rate]),
                        min_batch_risk=plan.risk_usd * min(COUNTS[p.rate]),
                        open_risk=e.risk,
                        portfolio_cap=self.lim.max_open_risk_usd,
                    )
                raise
        else:
            n = remaining
            why = room(self.lim, e, plan, n) or funding_veto(n)
            if why:
                raise Block(why)
        return n, plan, q, i

    def close_owned(self, pos, reason, *, intent=None):
        if not self.armed or not self.owned(pos):
            return
        mt = self.mt
        self.account()
        active = mt.positions_get(ticket=pos.ticket)
        if active is None:
            raise DemoSafetyError("Close ticket verification unavailable")
        if (
            len(active) != 1
            or not self.owned(active[0])
            or active[0].symbol != pos.symbol
        ):
            return
        p = active[0]
        if p.type != pos.type or (intent and not self._matches_exit(p, intent)):
            return
        q = mt.symbol_info_tick(p.symbol)
        i = mt.symbol_info(p.symbol)
        if i is None or not fresh(q, time.time(), self.lim.max_quote_age_s):
            return
        if intent and (
            q.time_msc < intent["trigger_msc"]
            or q.time_msc <= intent.get("last_tick", -1)
        ):
            return  # wait for a fresh post-signal executable quote; never widen the stop
        buy = p.type != mt.POSITION_TYPE_BUY
        req = {
            "action": mt.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "position": p.ticket,
            "volume": p.volume,
            "type": self.typ(1 if buy else -1),
            "price": q.ask if buy else q.bid,
            "deviation": 0,
            "magic": self.h.magic,
            "type_time": mt.ORDER_TIME_GTC,
            "type_filling": _filling_mode(mt, i),
            "comment": "hull_exit",
        }
        self.account()
        if intent is not None:
            # Write ahead of the send. A crash/unknown ack must not send a duplicate close.
            intent.update(
                status="verify_or_review", last_tick=q.time_msc, volume_sent=p.volume
            )
            self.persist()
        res = mt.order_send(req)
        if intent is not None:
            code = getattr(res, "retcode", None)
            intent["retcode"] = code
            if code == getattr(mt, "TRADE_RETCODE_DONE_PARTIAL", 10010):
                intent["status"] = "verify_partial"
            elif code in (10004, 10018, 10020, 10021, 10024):
                # Definite no-fill: requote, closed market, price changed/off, throttled.
                intent["status"] = "await_quote"
            self.persist()
        self.emit(
            "exit_request",
            symbol=p.symbol,
            ticket=int(p.ticket),
            reason=reason,
            tf=self.owners.lane(p),
            retcode=getattr(res, "retcode", None),
        )

    def _matches_exit(self, p, x):
        return (
            x.get("tf") is not None
            and self.owners.lane(p) == x["tf"]
            and p.symbol == x["symbol"]
            and p.type == x["type"]
            and getattr(p, "identifier", p.ticket) == x["identifier"]
            and p.price_open == x["open"]
        )

    def _service_exit(self, p, x):
        key = str(p.ticket)
        if not self.owned(p) or not self._matches_exit(p, x):
            if x["status"] != "identity_review":
                x["status"] = "identity_review"
                self.persist()
                self.emit(
                    "opposite_exit_review",
                    symbol=x["symbol"],
                    ticket=p.ticket,
                    reason="identity_changed",
                )
            return
        if x["status"] == "verify_partial" and 0 < p.volume < x["volume_sent"]:
            x["status"] = "await_quote"  # only the reconciled remaining volume
        if x["status"] == "await_quote":
            self.close_owned(p, x["reason"], intent=x)
            after = self.mt.positions_get(ticket=p.ticket)
            if after is not None and not after:
                self.exits.pop(key, None)
                self.persist()
                self.emit(
                    "lane_exit_confirmed",
                    symbol=p.symbol,
                    tf=x.get("tf"),
                    reason=x.get("reason"),
                    ticket=p.ticket,
                )
            elif x["status"] == "verify_or_review":
                self.emit(
                    "opposite_exit_review",
                    symbol=p.symbol,
                    ticket=p.ticket,
                    retcode=x.get("retcode"),
                )

    def close_opposite(self, name, reading):
        """Latch and immediately service raw opposite signals. No entry/stop/zone gate."""
        now = time.time()
        if (
            not self.armed
            or not isinstance(reading, Reading)
            or reading.label not in (-1, 1)
            or TF_NAME.get(reading.tf) not in self.h.eligible_timeframes
            or not 0 <= now - reading.signal_s <= self.lim.max_entry_age_s
            or (reading.trigger is not None and now >= reading.close_s)
        ):
            return 0
        return self._protect(name, reading.tf, reading.signal_msc, reading.label)

    def close_mss(self, name, reading):
        """Confirmed native structure shift. Fence the lane before any replacement.

        Not an entry: no entry-age cutoff, raw cross, valid stop, spread cap or
        market-session gate. Reserve all matching legs durably before sending.
        """
        if not isinstance(reading, Reading) or not reading.confirmed_mss:
            return 0
        return self.confirmed_shift(name, reading.tf, reading.structure)

    def confirmed_shift(self, name, tf, frame):
        close = frame.closed_s
        if (
            close is None
            or close > time.time()
            or name not in self.syms
            or TF_NAME.get(tf) not in self.h.eligible_timeframes
            or frame.bias not in (-1, 1)
            or frame.event_code != 2 * frame.bias
        ):
            return 0
        seen = self.mss_book.setdefault(name, {})
        if seen.get(str(tf), -1) >= close:
            return 0
        window = self.waiting.get((name, tf))
        if window and window.reading.signal_s <= close:
            self.cancel_wait(name, tf, "confirmed_mss_exit")
        # Write the lane/time fence BEFORE querying inventory. Even if the terminal
        # is unavailable or the process stops here, supervise() can latch an older
        # verified ticket, never one opened after this shift.
        seen[str(tf)] = close
        self.persist()
        return self._protect(name, tf, close * 1000, 0, bias=frame.bias)

    def _protect(self, name, tf, stamp, raw_side, *, bias=0):
        shift = raw_side == 0
        close = stamp // 1000
        ps = self.mt.positions_get() if self.armed else ()
        if ps is None:
            if shift:
                self.emit(
                    "lane_mss_await_inventory",
                    symbol=name,
                    tf=tf,
                    close_s=close,
                )
                return 0
            raise DemoSafetyError("Cannot verify owned positions for lane exit")
        matched = []
        reason = "confirmed_lane_mss" if shift else "opposite_hull_label"
        for p in ps:
            if (
                p.symbol != name
                or not self.owned(p)
                or self.owners.lane(p) != tf
                or (not shift and p.type == self.typ(raw_side))
                or not 0 < opened_ms(p) <= stamp
            ):
                continue
            self._reserve_exit(p, tf, reason, stamp)
            matched.append(p)
        if matched or shift:
            self.persist()  # watermark and every ticket intent in one atomic snapshot
        for p in matched:
            self._service_exit(p, self.exits[str(p.ticket)])
        if shift:
            self.emit(
                "lane_mss_exit",
                symbol=name,
                tf=tf,
                close_s=close,
                bias=bias,
                matched_positions=len(matched),
                armed=self.armed,
            )
        return len(matched)

    def _reserve_exit(self, p, tf, reason, stamp):
        self.exits.setdefault(
            str(p.ticket),
            {
                "symbol": p.symbol,
                "tf": tf,
                "reason": reason,
                "type": p.type,
                "open": p.price_open,
                "identifier": getattr(p, "identifier", p.ticket),
                "trigger_msc": stamp,
                "status": "await_quote",
            },
        )

    def service_if_due(self):
        # Single MT5 writer: also check between confirmed legs / symbols when a
        # batch or scan is slow. Never interleave amendments into an entry send.
        if (
            self.armed
            and (self.ladders.book or self.exits)
            and time.monotonic() - self.last_service >= FAST_POLL_S
        ):
            self.supervise()

    def supervise(self):
        if not self.armed:
            return
        ps, _ = snapshot(self.mt)
        goals = self.state.setdefault("target_by_ticket", {})
        active = {
            str(p.ticket) for p in ps
        }  # Never drop a goal solely because a profile was deselected.
        quotes, grids, plans = (
            {},
            {},
            {},
        )  # per sweep only; fresh quote is re-read before each send
        # Reconcile outstanding shift fences even after restart, on one full
        # account snapshot, before a ladder amendment or a new entry is possible.
        recovered = False
        for p in ps:
            if self.owned(p) and str(p.ticket) not in self.exits:
                tf = self.owners.lane(p)
                stamp = self.mss_book.get(p.symbol, {}).get(str(tf), -1) * 1000
                if tf is not None and 0 < opened_ms(p) <= stamp:
                    self._reserve_exit(p, tf, "confirmed_lane_mss", stamp)
                    recovered = True
        if recovered:
            self.persist()
        for p in ps:
            if not self.owned(p):
                continue
            key = str(p.ticket)
            if key in self.exits:
                self._service_exit(p, self.exits[key])
                continue  # never amend a ticket with a pending close
            is_ladder = key in self.ladders.book
            goal = goals.get(key) if not is_ladder else None
            why = (
                "kill"
                if self.kill.exists()
                else "missing_protection"
                if not (p.sl > 0 and p.tp > 0)
                else "cash_target"
                if goal and p.profit + 1e-9 >= goal  # same USD tolerance as TP pricing
                else ""
            )
            if why:
                self.close_owned(p, why)
            elif is_ladder:
                self.ladders.supervise(p, quotes, grids, plans)
        for key in set(self.exits) - active:
            pending = self.exits.pop(key)
            self.persist()
            self.emit(
                "lane_exit_confirmed",
                symbol=pending["symbol"],
                tf=pending.get("tf"),
                reason=pending.get("reason"),
                ticket=int(key),
            )
        if self.owners.prune(
            {str(getattr(p, "identifier", p.ticket)) for p in ps}, time.time()
        ):
            self.persist()
        self.ladders.prune(active)
        if stale := set(goals) - active:
            for k in stale:
                del goals[k]
            self.persist()
        self.last_service = time.monotonic()

    def finalize(self, name, tf, plan, res, expected):
        mt = self.mt
        self.account()
        if (
            res is None
            or res.retcode != mt.TRADE_RETCODE_DONE
            or not math.isclose(float(res.volume), plan.lots, abs_tol=1e-8)
        ):
            return None, "uncertain_or_partial_fill"
        ps, os = snapshot(mt)
        new = [
            p
            for p in ps
            if p.symbol == name
            and p.ticket not in expected
            and self.owners.lane(p) is None
        ]
        if len(new) != 1 or not self.owned(new[0]):
            return None, "ticket_not_reconciled"
        p = new[0]
        # On a hedging account the opening order identifies the new position.
        # If MT5 supplies that identifier, never enroll a different ticket merely
        # because it appeared in the same positions snapshot.
        opening_order = getattr(res, "order", 0)
        if not opening_order or opening_order not in (
            p.ticket,
            getattr(p, "identifier", p.ticket),
        ):
            return None, "opening_order_not_reconciled"
        grid = Grid.from_info(mt.symbol_info(name))
        tol = grid.tick * 1e-6
        if (
            p.type != self.typ(plan.side)
            or abs(p.volume - plan.lots) > 1e-8
            or not math.isfinite(p.price_open)
            or p.price_open <= 0
        ):
            return None, "fill_not_reconciled"
        if not self.owners.bind(p, tf):
            return None, "lane_identity_not_reconciled"
        self.persist()
        if self.h.exit_policy == FIXED:
            self.state.setdefault("target_by_ticket", {})[str(p.ticket)] = plan.goal_usd
            self.persist()
        if abs(p.sl - plan.sl) > tol or not (p.sl > 0 and p.tp > 0):
            self.close_owned(p, "unverified_protection")
            return None, "protection_mismatch"
        # Always inspect the broker snapshot for foreign/unprotected exposure,
        # pending orders and valid pricing, even without a discretionary dollar cap.
        filled_exposure = self.exposure(ps, os)
        cap = self.lim.max_open_risk_usd
        if cap is not None and filled_exposure.risk > cap + 1e-7:
            self.close_owned(p, "aggregate_fill_risk")
            return None, "fill_risk_exceeded"
        tp, _ = target(
            grid,
            plan.side,
            plan.lots,
            p.price_open,
            plan.goal_usd,
            self.cash(name),
            cash_per_price=plan.reward_usd / abs(plan.tp - plan.entry),
        )
        if abs(tp - p.tp) > tol:
            # Anchor to the actual fill. Do not re-sample or double the frozen spread cushion.
            if self.h.exit_policy == FIXED and p.profit + 1e-9 >= plan.goal_usd:
                self.close_owned(p, "cash_target_after_fill")
                return None, "closed_during_batch"
            if self.h.exit_policy == LADDER:
                q = mt.symbol_info_tick(name)
                if not fresh(q, time.time(), self.lim.max_quote_age_s):
                    return None, "initial_target_alignment_quote_unavailable"
                mark = executable(plan.side, q.bid, q.ask)
                if (
                    not inside(plan.side, mark, Protection(0, p.sl, p.tp))
                    or plan.side * (tp - mark) < max(grid.gap, grid.tick) - tol
                ):
                    return None, "initial_target_alignment_not_executable"
            req = {
                "action": mt.TRADE_ACTION_SLTP,
                "symbol": name,
                "position": p.ticket,
                "sl": plan.sl,
                "tp": tp,
                "magic": self.h.magic,
            }
            check = mt.order_check(req)
            if check is None or check.retcode != 0:
                return None, "target_alignment_rejected"
            self.account()
            rr = mt.order_send(req)
            verify = mt.positions_get(ticket=p.ticket)
            if (
                rr is None
                or rr.retcode != mt.TRADE_RETCODE_DONE
                or not verify
                or len(verify) != 1
                or not self.owned(verify[0])
                or verify[0].ticket != p.ticket
                or verify[0].symbol != name
                or verify[0].type != p.type
                or verify[0].price_open != p.price_open
                or verify[0].volume != p.volume
                or abs(verify[0].tp - tp) > tol
                or abs(verify[0].sl - plan.sl) > tol
            ):
                return None, "target_alignment_unverified"
        if self.h.exit_policy == LADDER:
            # Do not enroll a pre-existing, partial, unknown or unverified ticket.
            self.ladders.enroll(p, plan, tp)
        return p.ticket, "ok"

    def cancel_wait(self, name, tf, reason):
        w = self.waiting.pop((name, tf), None)
        if w is not None:
            self.emit(
                "label_decision",
                symbol=name,
                tf=w.reading.tf,
                raw_selected_tf=w.raw_tf,
                close_s=w.reading.close_s,
                **w.reading.clock_fields(),
                outcome=reason,
                signal_session=self.session_gate(name).report(w.reading.signal_s),
                polls=w.polls,
                previous_wait=w.last_wait_reason,
            )

    def cancel_all_waits(self):
        for name, tf in list(self.waiting):
            self.cancel_wait(name, tf, "first_quote_wait_stopped")

    def _live_reserved(self, name, d):
        # Honor pre-upgrade live reservations for duplicate prevention. Never write a new one.
        last = self.state.get("live_bars", {}).get(name, {}).get(str(d.tf))
        return last == [d.close_s, d.side]

    def batch(self, name, d, *, raw_tf=None):
        if name not in self.syms:
            return "symbol_not_selected"
        if not isinstance(d, Reading):
            return "not_dynamic_hull_signal"
        if TF_NAME.get(d.tf) not in self.h.eligible_timeframes:
            return "timeframe_not_eligible"
        if not d.entry_side:
            return d.entry_reason
        bars = self.state.setdefault("dynamic_entry_bars", {}).setdefault(name, {})
        bar_id = f"{d.tf}:{d.side}"
        if bars.get(bar_id, -1) >= d.close_s:
            return "already_consumed"
        seen = self.state.setdefault("lane_consumed", {}).setdefault(name, {})
        if self.mss_book.get(name, {}).get(str(d.tf), -1) >= d.signal_s:
            return "confirmed_mss_exit"
        # Legacy symbol watermark is read-only; NEW reservations never consume siblings.
        if max(
            seen.get(str(d.tf), 0), self.state.get("consumed", {}).get(name, 0)
        ) >= d.signal_s or self._live_reserved(name, d):
            return "already_consumed"
        self.cancel_wait(name, d.tf, "first_quote_wait_superseded")
        # Reserve ONCE before touching broker order APIs. Only the original,
        # in-memory first-quote window may resume this reservation. A restart cannot.
        seen[str(d.tf)] = d.signal_s
        bars[bar_id] = d.close_s
        self.persist()
        mono = time.monotonic()
        remaining = d.signal_s + self.lim.max_entry_age_s - time.time()
        window = FirstQuoteWindow(d, mono + remaining, raw_tf)
        return self._attempt(name, window)

    def resume_first_quote(self, name, tf):
        # Take the reservation out before processing; only another waitable check can put it back.
        w = self.waiting.pop((name, tf))
        if self.state["lane_consumed"][name].get(str(tf)) != w.reading.signal_s:
            return "first_quote_wait_superseded"
        return self._attempt(name, w)

    def _attempt(self, name, window):
        d = window.reading
        window.polls += 1
        expected = set()
        n = None
        sent = 0
        recheck = None  # one bounded *unsent* reprice per leg, never a send retry
        try:
            while True:
                left, plan, q, i = self.prepare(
                    name,
                    d,
                    window,
                    "tick_reprice"
                    if recheck is not None
                    else "first_plan"
                    if n is None
                    else "next_leg",
                    expected,
                    None if n is None else n - sent,
                )
                if recheck is None:
                    base_bid, base_ask = q.bid, q.ask
                    base_grid = Grid.from_info(i)
                    tolerance = base_grid.tick * (1 + 1e-9)
                elif (q.bid, q.ask) != recheck or Grid.from_info(i) != base_grid:
                    raise Block("final_quote_or_safety_changed")
                if n is None:
                    n = left
                    preview_s = time.time()
                    self.session_gate(name).require("batch_preview", decision=preview_s)
                    self.emit(
                        "batch_preview",
                        symbol=name,
                        tf=d.tf,
                        signal=d.signal_s,
                        rate=self.syms[name][1].rate,
                        legs=n,
                        entry_session=self.session_gate(name).report(preview_s),
                        exit_policy=self.h.exit_policy,
                        target_policy=TARGET_POLICY,
                        goal_per_leg=plan.goal_usd,  # indicative first leg, not a promise for siblings
                        risk_batch=n * plan.risk_usd,
                        **d.clock_fields(),
                        **asdict(plan),
                    )
                    if not self.armed:
                        return "shadow_plan_only"
                req = {
                    "action": self.mt.TRADE_ACTION_DEAL,
                    "symbol": name,
                    "type": self.typ(d.side),
                    "volume": plan.lots,
                    "price": plan.entry,
                    "sl": plan.sl,
                    "tp": plan.tp,
                    "deviation": 0,
                    "magic": self.h.magic,
                    "type_time": self.mt.ORDER_TIME_GTC,
                    "type_filling": _filling_mode(self.mt, i),
                    "comment": (
                        f"i{d.tf}_{d.trigger.ms}_{sent + 1}"
                        if d.trigger
                        else f"h{d.tf}_{d.close_s}_{sent + 1}"
                    ),
                }
                chk = self.mt.order_check(req)
                a = self.account()
                retcode = getattr(chk, "retcode", None)
                if retcode != 0:
                    self.emit(
                        "broker_precheck_rejected",
                        symbol=name,
                        tf=d.tf,
                        signal=d.signal_s,
                        leg=sent + 1,
                        retcode=retcode,
                    )
                    raise Block("broker_precheck_rejected")
                if why := self._margin_check(
                    name, d, a, "order_check", 1, getattr(chk, "margin_free", None)
                ):
                    raise Block(why)
                # Do not send the inspected plan after a change in exposures/quote, a kill, or a stale signal.
                _, again, q2, i2 = self.prepare(
                    name, d, window, "pre_send", expected, n - sent
                )
                # Remote one-tick tolerance, anchored to the ORIGINAL quote for this
                # leg. Both sides must fit, AND the broker-checked plan must still match.
                checked_prices = (q2.bid, q2.ask)
                if (
                    max(abs(q2.bid - base_bid), abs(q2.ask - base_ask)) > tolerance
                    or Grid.from_info(i2) != base_grid
                ):
                    raise Block("quote_or_plan_changed")
                if recheck is not None and checked_prices != recheck:
                    raise Block("final_quote_or_safety_changed")
                if again != plan:
                    # A move on just the non-entry side can now change the goal.
                    # One unsent reprice inside the original one-tick band, sharing
                    # the final-quote budget. Never patch a broker-checked request.
                    if (
                        recheck is None
                        and checked_prices != (q.bid, q.ask)
                        and again.entry == plan.entry
                        and again.sl == plan.sl
                        and again.risk_usd == plan.risk_usd
                        and again.entry_spread_usd != plan.entry_spread_usd
                    ):
                        recheck = checked_prices
                        continue
                    raise Block("quote_or_plan_changed")
                self.state["manual_hold"] = {
                    "symbol": name,
                    "tf": d.tf,
                    "signal": d.signal_s,
                    **d.clock_fields(),
                    "leg": sent + 1,
                    "target_policy": TARGET_POLICY,
                    "initial_goal_usd": plan.goal_usd,
                    "entry_spread_usd": plan.entry_spread_usd,
                }
                self.persist()
                self.account()
                try:
                    q3, _ = self._entry_quote(name, window, "final_send")
                    final_prices = (q3.bid, q3.ask)
                    if (
                        self.kill.exists()
                        or max(abs(q3.bid - base_bid), abs(q3.ask - base_ask))
                        > tolerance
                        or (recheck is not None and final_prices != checked_prices)
                    ):
                        raise Block("final_quote_or_safety_changed")
                    gate = self.session_gate(name)
                    checked = self._quote_clock(gate, window, q3, "final_send").checked
                    gate.require("final_send", decision=checked)
                except (Block, EntrySessionVeto):
                    # No send has happened on this leg. Do not requeue after a veto.
                    self.state.pop("manual_hold")
                    self.persist()
                    raise
                if final_prices != checked_prices:
                    # One tick can still cross a spread cap, stop distance or
                    # funding limit. Rebuild and broker-check the actual new request
                    # once, at this exact quote, on the same signal/deadline. Never
                    # edit the price of an already checked request and send it blind.
                    recheck = final_prices
                    self.state.pop("manual_hold")  # no send was made
                    self.persist()
                    continue
                res = self.mt.order_send(req)
                self.emit(
                    "demo_result",
                    symbol=name,
                    leg=sent + 1,
                    signal=d.signal_s,
                    request=req,
                    **d.clock_fields(),
                    send_check_utc=utc(checked),
                    entry_session=self.session_gate(name).report(checked),
                    send_tick_time_msc=int(q3.time_msc),
                    send_bid=q3.bid,
                    send_ask=q3.ask,
                    target_policy=TARGET_POLICY,
                    configured_target_usd=plan.configured_target_usd,
                    entry_spread_usd=plan.entry_spread_usd,
                    initial_goal_usd=plan.goal_usd,
                    planned_tp_profit_usd=plan.reward_usd,
                    retcode=getattr(res, "retcode", None),
                    price=getattr(res, "price", None),
                    deal=getattr(res, "deal", None),
                )
                ticket, status = self.finalize(name, d.tf, plan, res, expected)
                if ticket is None:
                    return status + "_manual_hold"
                expected.add(ticket)
                sent += 1
                recheck = None
                self.state.pop("manual_hold")
                self.persist()
                if sent == n:
                    return "demo_batch_sent"
                self.service_if_due()
        except TimingBlock as exc:
            check = exc.check
            wait = (
                exc.stage == "first_plan" and n is None and not sent and check.waitable
            )
            if not wait or window.last_wait_reason != check.reason:
                self.emit(
                    "entry_quote_check",
                    symbol=name,
                    tf=d.tf,
                    signal=d.signal_s,
                    stage=exc.stage,
                    polls=window.polls,
                    disposition="wait" if wait else "reject",
                    previous_wait=window.last_wait_reason,
                    **check.audit(d.tf),
                )
            if wait:
                window.last_wait_reason = check.reason
                self.waiting[(name, d.tf)] = window
                return WAITING
            why = check.reason
        except EntrySessionVeto as exc:
            why = str(exc)
            self.emit(
                "entry_session_veto",
                symbol=name,
                tf=d.tf,
                signal=d.signal_s,
                reason=why,
                profile=self.syms[name][0],
                legs_already_sent=sent,
                **exc.fields,
            )
        except Block as exc:
            why = str(exc)
        if why == "manual_reconciliation_required":
            return why
        return (
            "manual_hold:"
            if self.state.get("manual_hold")
            else "protected_partial_no_topup:"
            if sent
            else ""
        ) + why
