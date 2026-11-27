"""Bounded demo batches with durable no-retry intent and broker-side SL and TP on EVERY leg."""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from bot.broker import (
    DemoSafetyError,
    _filling_mode,
    _side_is_allowed,
    account_check,
    fresh,
    save,
    snapshot,
)
from bot.config.entry_sessions import EntrySessionVeto, sessions_for_runner
from bot.hull.hours import Hours

from .config import (
    COUNTS,
    FIXED,
    INTRABAR,
    LADDER,
    PAIR_MODE,
    RETEST,
    TARGET_POLICY,
    TF_NAME,
)
from .entry_clock import FAST_POLL_S, WAITING, TimingBlock, check_quote, utc
from .intrabar_guard import OriginGuards
from .ladder import Protection, executable, inside
from .ladder_desk import TicketLadders, opened_ms
from .orders import (
    Block,
    Exposure,
    Grid,
    count_for,
    make_plan,
    quote_spread,
    room,
    target,
    valid_cash,
)
from .ownership import Owners
from .retest import make_retest_plan
from .retest_desk import RETEST_MARKET_FALLBACK_REASONS, RetestDesk
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


class AwaitOwnedFlat(Block):
    """Only an unsent, independently confirmed close can wait for this."""


@dataclass(slots=True)
class FirstQuoteWindow:
    reading: Reading
    end_mono: float
    raw_tf: int | None
    polls: int = 0
    last_wait_reason: str | None = None


class BatchLevel:
    """For a verified paired market reservation only; never a send retry budget.

    The entry price and a SELL's Ask translation may move. The selected level,
    contract, size, cash base and original signal/deadline may not. Every changed
    price needs a strictly later broker millisecond, so even a stalled clock
    cannot spin indefinitely inside a finite entry window.
    """

    def __init__(self):
        self.binding = self.last = None

    def observe(self, q, grid, plan):
        binding = (
            grid,
            plan.side,
            plan.lots,
            plan.configured_target_usd,
            plan.raw_chart_sl,
            plan.cross_region_raw_stop,
            plan.cross_region_action,
            plan.stop_spread_policy,
        )
        if self.binding is not None and binding != self.binding:
            raise Block("batch_level_or_contract_changed")
        self.binding = binding
        tick = (q.time_msc, q.bid, q.ask)
        if self.last and (
            tick[0] < self.last[0] or (tick[0] == self.last[0] and tick != self.last)
        ):
            raise Block("ambiguous_or_reversed_batch_tick")
        self.last = tick


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
        self.open_lanes = (
            set()
        )  # refreshed by verified inventory; used only to schedule protective reads
        self.watch_other_symbols = None
        self.indicator_key = None
        self.verify_signal = (
            None  # installed by the native scanner; mandatory for live orders
        )
        self.exits = self.state.setdefault("opposite_exits", {})
        self.owners = Owners(state, pairs=h.pairs)
        self.origins = OriginGuards(self)
        self.mss_book = state.setdefault("last_lane_mss_exit", {})
        self.retest = RetestDesk(self) if h.entry_route == RETEST else None
        if armed and self.retest is None and state.get("retest_batches_v1"):
            raise DemoSafetyError(
                "Unresolved retest orders need their original manager; do not switch entry routes"
            )
        self.waiting: dict[
            tuple[str, int], FirstQuoteWindow
        ] = {}  # never restored after restart

    def persist(self):
        if self.armed:
            save(self.path, self.state)

    @staticmethod
    def now():
        return time.time()

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
        if os and not self.retest:
            raise Block("pending_orders_present")
        pending = self.retest.reserved(os, ps) if self.retest else Exposure()
        risk = 0.0
        for p in ps:
            if not self.owned(p) or not (p.sl > 0 and p.tp > 0):
                raise Block("foreign_or_unprotected_exposure")
            side = 1 if p.type == self.mt.POSITION_TYPE_BUY else -1
            risk += max(
                0.0,
                -valid_cash(self.cash(p.symbol), side, p.volume, p.price_open, p.sl),
            )
        return Exposure(
            len(ps) + pending.count,
            sum(p.volume for p in ps) + pending.lots,
            risk + pending.risk,
        )

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
            intrabar=d.intrabar,
        )
        if d.intrabar and (
            check.checked >= d.close_s or (check.tick_msc or 0) >= d.close_s * 1000
        ):
            raise Block("origin_candle_expired")
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
        if self.verify_signal is not None:
            # Verification can service a *protective* raw opposite / MSS between
            # legs. Never discard a usable closing-edge tick before it does so.
            verified = self.verify_signal(name, d, q)
            gate.require(stage, decision=check.checked, quote=check.tick_msc / 1000)
            if not verified:
                raise Block("live_cross_or_context_unverified" if d.intrabar else "next_bar_quote_or_context_unverified")
            # A newer tick may have arrived inside a two-chart verification.
            # Legacy boolean verifiers certify q; the paired book returns the
            # exact stable tick it certified. Never price a mixed snapshot.
            if verified is not True:
                q = verified
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
                **({"trigger_tf": d.candle_tf} if d.paired else {}),
                **check.audit(d.candle_tf, live=d if d.intrabar else None),
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
            ):
                continue
            if not reading.intrabar and deal.type != closing_type:
                if self._closed_fallback(reading):
                    ms = getattr(deal, "time_msc", None)
                    if type(ms) is not int or ms <= 0:
                        raise Block("entry_deal_history_unavailable")
                    if (
                        ms >= reading.signal_msc
                        and self.owners.deal_lane(deal, self.syms, self.h.magic) is None
                    ):
                        raise Block("post_close_exit_ownership_unverified")
                continue  # independent exit of an opposite position, not a repeat entry
            # MT5 can assign a manual or server exit a different magic. Resolve the
            # actual opening position ID. An unknown recent exit remains a safety veto.
            owner = self.owners.book.get(str(getattr(deal, "position_id", None)))
            if (
                owner
                and owner["symbol"] == name
                and owner["magic"] == self.h.magic
                and reading.trigger_tf is None
                and owner["tf"] != reading.tf
            ):
                continue
            ms = getattr(deal, "time_msc", None)
            if not isinstance(ms, int) or ms <= 0:
                raise Block("entry_deal_history_unavailable")
            if ms >= reading.signal_msc:
                raise Block("prior_batch_not_flat_at_cross" if reading.intrabar else "same_side_batch_at_signal_close")

    def _closed_fallback(self, r):
        return (
            self.h.effective_timing == INTRABAR
            and self.h.execution_mode == PAIR_MODE
            and not self.retest
            and r.paired
            and not r.intrabar
            and r.trigger is None
            and r.cross_bar is not None
            and self.h.pairs.get(r.tf) == r.trigger_tf
        )

    def _own_opposite_drain(self, name, r, issues, positions):
        """Only park against actual, independently reserved, attributable opposing exits.

        A ticket can disappear between its individual read-back and the full
        snapshot. In that case still WAIT for supervision, never assume flat.
        This routine does not create, send, retry or clear any exit.
        """
        here = {str(p.ticket): p for p in positions}
        if not issues or not here.keys() <= issues.keys():
            return False
        for key, x in issues.items():
            rec = self.owners.book.get(str(x["identifier"])) or {}
            parent = rec.get("tf")
            if (
                str(rec.get("ticket")) != key
                or rec.get("symbol") != name
                or rec.get("magic") != self.h.magic
                or rec.get("identifier") != x["identifier"]
                or rec.get("type") != self.typ(-r.side)
                or parent not in self.h.pairs
                or rec.get("trigger_tf") != self.h.pairs[parent]
                or x.get("tf") != parent
                or x.get("trigger_tf", rec["trigger_tf"]) != rec["trigger_tf"]
                or x.get("type") != rec["type"]
                or x.get("open") != rec["open"]
                or not 0 < rec["opened_msc"] <= r.signal_msc
                or x.get("status")
                not in ("await_quote", "verify_partial", "verify_or_review")
            ):
                return False
            p = here.get(key)
            if p is not None and (not self.owned(p) or not self._matches_exit(p, x)):
                return False
        return True

    def prepare(
        self, name, d, window, stage, expected=None, remaining=None, *, force_market=False
    ):
        if TF_NAME.get(d.tf) not in self.h.eligible_timeframes:
            raise Block("timeframe_not_eligible")
        if not d.entry_side:
            raise Block(d.entry_reason)
        if (
            d.paired
            and self.pair_fence(name, d.tf, d.trigger_tf, -d.side) >= d.signal_msc
        ):
            raise Block("opposite_after_pair_cross")
        if d.trigger_tf == 1 and self.minute_invalidated(name, d):
            raise Block("opposite_after_m1_cross")
        if self.mss_book.get(name, {}).get(str(d.tf), -1) >= d.signal_s:
            raise Block("confirmed_mss_exit")
        if (
            d.intrabar
            and self.state.get("intrabar_flat_through_ms", {}).get(name, -1)
            >= d.signal_msc
        ):
            raise Block("prior_batch_not_flat_at_cross")
        issues = {
            k: x
            for k, x in self.exits.items()
            if x["symbol"] == name
            and (d.trigger_tf is not None or x.get("tf") in (None, d.tf))
        }
        if issues and (stage != "first_plan" or not self._closed_fallback(d)):
            raise Block("opposite_exit_not_confirmed_flat")
        mt = self.mt
        p = self.syms[name][1]
        a = self.account()
        now = time.time()
        # Entries use the confirmed cross close, not an earlier intrabar projection.
        self.session_gate(name).require(stage, signal=d.signal_s)
        if p.session == "configured" and (why := self.hours.status(now)[0]):
            raise Block(why)
        if self.kill.exists():
            raise Block("kill")
        if self.state.get("manual_hold"):
            raise Block("manual_reconciliation_required")
        if self.ladders.needs_review():
            raise Block("ladder_reconciliation_required")
        if self.origins.unreconciled():
            raise Block("cross_stop_ack_pending_review")
        ps, os = snapshot(mt)
        e = self.exposure(ps, os)
        here = [(r, self.owners.lane(r)) for r in ps if r.symbol == name]
        if issues:
            if self._own_opposite_drain(name, d, issues, [p for p, _ in here]):
                if (
                    now > d.signal_s + self.lim.max_entry_age_s
                    or time.monotonic() > window.end_mono
                ):
                    raise Block("entry_window_expired")
                if now >= d.close_s + d.candle_tf * 60:
                    raise Block("successor_candle_expired")
                self._entry_quote(name, window, "opposite_flat_wait")
                raise AwaitOwnedFlat("waiting_verified_opposite_flat")
            raise Block("opposite_exit_not_confirmed_flat")
        if any(tf is None for _, tf in here):
            raise Block("unattributed_hull_position_review")
        if d.trigger_tf == 1 and any(
            tf != d.tf or self.owners.minute_source(r) != d.active_source
            for r, tf in here
        ):
            raise Block("symbol_busy_or_batch_changed")
        if d.paired and any(
            tf != d.tf or self.owners.origin(p).get("trigger_tf") != d.trigger_tf
            for p, tf in here
        ):
            raise Block("symbol_busy_or_batch_changed")
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
        if d.paired and getattr(i, "chart_mode", None) != getattr(
            mt, "SYMBOL_CHART_MODE_BID", 0
        ):
            raise Block("unverified_bid_chart")
        grid = Grid.from_info(i)
        if not _side_is_allowed(mt, i, "BUY" if d.side == 1 else "SELL"):
            raise Block("broker_side_disabled")
        q, _ = self._entry_quote(name, window, stage)
        plan = (
            make_retest_plan(d, p, grid, q.bid, q.ask, self.cash(name), self.h)
            if (self.retest and not force_market)
            else make_plan(d, p, grid, q.bid, q.ask, self.cash(name))
        )
        margin = _finite_or_none(
            mt.order_calc_margin(self.typ(d.side), name, p.lots, plan.entry)
        )
        free = _finite_or_none(getattr(a, "margin_free", None))
        min_legs = min(COUNTS[p.rate]) if remaining is None else remaining

        rows, unfunded = (
            self.retest.pending_terms(a, os, ps) if self.retest else ((), 0.0)
        )

        def funding_veto(n):
            if self.retest and (
                veto := self.retest.broker_room(rows, name, d.side, plan, n, a, i)
            ):
                return veto
            projected = (
                free - unfunded - n * margin
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
            **(
                {"trigger_tf": intent["trigger_tf"]}
                if intent and "trigger_tf" in intent
                else {}
            ),
            tf=self.owners.lane(p),
            retcode=getattr(res, "retcode", None),
        )

    def _matches_exit(self, p, x):
        return (
            x.get("tf") is not None
            and self.owners.lane(p) == x["tf"]
            and (
                "trigger_tf" not in x
                or self.owners.origin(p).get("trigger_tf") == x["trigger_tf"]
            )
            and p.symbol == x["symbol"]
            and p.type == x["type"]
            and getattr(p, "identifier", p.ticket) == x["identifier"]
            and p.price_open == x["open"]
        )

    def _mark_flat_check(self, x):
        if self.h.effective_timing == INTRABAR:
            book = self.state.setdefault("intrabar_flat_through_ms", {})
            name = x["symbol"]
            rec = self.owners.book.get(str(x.get("identifier")))
            moment = max(int(time.time() * 1000), x.get("last_tick", -1))
            book[name] = max(book.get(name, -1), moment)
            if (
                rec
                and self.h.pairs.get(rec["tf"]) == rec.get("trigger_tf")
                and rec.get("trigger_tf")
            ):
                rec["flat_verified_msc"] = moment

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
                self._mark_flat_check(x)
                self.persist()
                self.emit(
                    "lane_exit_confirmed",
                    symbol=p.symbol,
                    tf=x.get("tf"),
                    reason=x.get("reason"),
                    **({"trigger_tf": x["trigger_tf"]} if "trigger_tf" in x else {}),
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
            (not self.armed and not self.retest)
            or not isinstance(reading, Reading)
            or reading.trigger_tf
            is not None  # never treat an attached child label as a parent cross
            or reading.label not in (-1, 1)
            or TF_NAME.get(reading.tf) not in self.h.eligible_timeframes
            or not 0 <= now - reading.signal_s <= self.lim.max_entry_age_s
            or (reading.trigger is not None and now >= reading.close_s)
        ):
            return 0
        if self.h.effective_timing == INTRABAR:
            fences = self.state.setdefault(
                "intrabar_parent_opposite_ms", {}
            ).setdefault(name, {})
            key = f"{reading.tf}:{reading.label}"
            if fences.get(key, -1) >= reading.signal_msc:
                return 0
            fences[key] = reading.signal_msc
            self.persist()
        return self._protect(name, reading.tf, reading.signal_msc, reading.label)

    def minute_fence(self, name, src, opposite):
        fences = self.state.get("m1_exit_fences", {}).get(name, {})
        return max(
            fences.get(f"{src}:{opposite}", -1), fences.get(f"all:{opposite}", -1)
        )

    def minute_invalidated(self, name, r):
        return self.minute_fence(name, r.active_source, -r.label) >= r.signal_msc

    def minute_opposite(self, name, signs, stamp, *, live, active_sources=()):
        """Latch verified raw signals before querying inventory; broker exits may complete later."""
        if (
            name not in self.syms
            or not signs
            or any(s not in (-1, 1) for s in signs.values())
        ):
            return 0
        fences = self.state.setdefault("m1_exit_fences", {}).setdefault(name, {})
        fresh = {
            src: side
            for src, side in signs.items()
            if fences.get(f"{src}:{side}", -1) < stamp
        }
        broadcast = {
            side
            for src, side in signs.items()
            if src in active_sources and fences.get(f"all:{side}", -1) < stamp
        }
        if not fresh and not broadcast:
            return 0
        fences.update({f"all:{side}": stamp for side in broadcast})
        fences.update({f"{src}:{side}": stamp for src, side in fresh.items()})
        self.persist()
        for key, window in list(self.waiting.items()):
            if (
                key[0] == name
                and window.reading.trigger_tf == 1
                and self.minute_invalidated(name, window.reading)
            ):
                self.cancel_wait(*key, "opposite_after_m1_cross")
        ps = self.mt.positions_get() if self.armed else ()
        matched = []
        for p in ps or ():
            if p.symbol != name or not self.owned(p) or not 0 < opened_ms(p) <= stamp:
                continue
            src = self.owners.minute_source(p)
            opposite = -1 if p.type == self.typ(1) else 1
            if src and (fresh.get(src) == opposite or opposite in broadcast):
                self._reserve_exit(p, self.owners.lane(p), "opposite_m1_hull", stamp)
                matched.append(p)
        if matched:
            self.persist()
        for p in matched:
            self._service_exit(p, self.exits[str(p.ticket)])
        self.emit(
            "m1_raw_exit_signal",
            symbol=name,
            trigger_tf=1,
            trigger_msc=stamp,
            phase="live" if live else "closed",
            crosses=fresh,
            broadcast_sides=sorted(broadcast),
            matched_positions=len(matched),
            inventory_available=ps is not None,
        )
        return len(matched)

    def pair_fence(self, name, parent, child, side):
        return (
            self.state.get("pair_exit_fences", {})
            .get(name, {})
            .get(f"{parent}:{child}:{side}", -1)
        )

    def pair_opposite(self, name, parent, r):
        """An observed native trigger cross belongs to exactly ONE parent/child pair."""
        if (
            name not in self.syms
            or self.h.execution_mode != PAIR_MODE
            or not isinstance(r, Reading)
            or r.trigger_tf is not None
            or self.h.pairs.get(parent) != r.tf
            or TF_NAME.get(parent) not in self.h.eligible_timeframes
            or r.label not in (-1, 1)
            or r.structure.closed_s
            != (r.close_s - r.tf * 60 if r.trigger else r.close_s)
            or r.signal_s > time.time()
            or (
                r.trigger is not None
                and (
                    time.time() >= r.close_s
                    or time.time() - r.signal_s > self.lim.max_quote_age_s
                )
            )
        ):
            return 0
        stamp = r.signal_msc
        if self.pair_fence(name, parent, r.tf, r.label) >= stamp:
            return 0
        self.state.setdefault("pair_exit_fences", {}).setdefault(name, {})[
            f"{parent}:{r.tf}:{r.label}"
        ] = stamp
        self.persist()  # write ahead: an unavailable inventory or a restart must not lose protection
        window = self.waiting.get((name, parent))
        if (
            window
            and window.reading.trigger_tf == r.tf
            and window.reading.label == -r.label
            and window.reading.signal_msc <= stamp
        ):
            self.cancel_wait(name, parent, "opposite_after_pair_cross")
        count = self._protect(name, parent, stamp, r.label, child=r.tf)
        self.emit(
            "pair_trigger_exit_signal",
            symbol=name,
            parent_tf=parent,
            trigger_tf=r.tf,
            raw_side=r.label,
            phase="live" if r.trigger else "closed",
            trigger_msc=stamp,
            matched_positions=count,
        )
        return count

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

    def _protect(self, name, tf, stamp, raw_side, *, bias=0, child=None):
        shift = raw_side == 0
        if self.retest:
            self.retest.interrupt(name, tf, stamp, raw_side, child=child)
        close = stamp // 1000
        ps = self.mt.positions_get() if self.armed else ()
        if ps is None:
            if shift or child is not None or self.h.effective_timing == INTRABAR:
                self.emit(
                    "lane_mss_await_inventory"
                    if shift
                    else "pair_exit_await_inventory",
                    symbol=name,
                    tf=tf,
                    close_s=close,
                )
                return 0
            raise DemoSafetyError("Cannot verify owned positions for lane exit")
        matched = []
        reason = (
            "opposite_pair_trigger_hull"
            if child is not None
            else "confirmed_lane_mss"
            if shift
            else "opposite_hull_label"
        )
        for p in ps:
            if (
                p.symbol != name
                or not self.owned(p)
                or self.owners.lane(p) != tf
                or (
                    child is not None
                    and self.owners.origin(p).get("trigger_tf") != child
                )
                or (not shift and p.type == self.typ(raw_side))
                or not 0 < opened_ms(p) <= stamp
            ):
                continue
            self._reserve_exit(p, tf, reason, stamp, child=child)
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

    def _reserve_exit(self, p, tf, reason, stamp, *, child=None):
        self.exits.setdefault(
            str(p.ticket),
            {
                "symbol": p.symbol,
                "tf": tf,
                "reason": reason,
                **({"trigger_tf": child} if child is not None else {}),
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
            and (
                self.ladders.book
                or self.exits
                or (self.retest and self.retest.active)
                or self.origins.has_pending_cross_stops()
            )
            and time.monotonic() - self.last_service >= FAST_POLL_S
        ):
            self.supervise()

    def supervise(self):
        if self.retest:
            self.retest.service()
        if not self.armed:
            return
        ps, _ = snapshot(self.mt)
        self.open_lanes = {(p.symbol, self.owners.lane(p)) for p in ps if self.owned(p)}
        if self.retest:
            self.open_lanes.update(self.retest.lanes())
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
                src = self.owners.minute_source(p)
                opp = -1 if p.type == self.typ(1) else 1
                m1stamp = self.minute_fence(p.symbol, src, opp) if src else -1
                child = (self.owners.origin(p) or {}).get("trigger_tf")
                pairstamp = (
                    self.pair_fence(p.symbol, tf, child, opp)
                    if child and self.h.pairs.get(tf) == child
                    else -1
                )
                parstamp = (
                    self.state.get("intrabar_parent_opposite_ms", {})
                    .get(p.symbol, {})
                    .get(f"{tf}:{opp}", -1)
                )
                if self.origins.reserve(p):
                    recovered = True
                elif 0 < opened_ms(p) <= pairstamp:
                    self._reserve_exit(
                        p, tf, "opposite_pair_trigger_hull", pairstamp, child=child
                    )
                    recovered = True
                elif tf is not None and 0 < opened_ms(p) <= parstamp:
                    self._reserve_exit(p, tf, "opposite_hull_label", parstamp)
                    recovered = True
                elif src and 0 < opened_ms(p) <= m1stamp:
                    self._reserve_exit(p, tf, "opposite_m1_hull", m1stamp)
                    recovered = True
                elif tf is not None and 0 < opened_ms(p) <= stamp:
                    self._reserve_exit(p, tf, "confirmed_lane_mss", stamp)
                    recovered = True
        if recovered:
            self.persist()
        self.origins.activate_pending(ps)
        ps, _ = snapshot(self.mt)
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
            self._mark_flat_check(pending)
            self.persist()
            self.emit(
                "lane_exit_confirmed",
                symbol=pending["symbol"],
                tf=pending.get("tf"),
                reason=pending.get("reason"),
                **(
                    {"trigger_tf": pending["trigger_tf"]}
                    if "trigger_tf" in pending
                    else {}
                ),
                ticket=int(key),
            )
        active_ids = {str(getattr(p, "identifier", p.ticket)) for p in ps}
        if self.h.effective_timing == INTRABAR:
            # A broker SL/TP/manual closure first discovered on this very tick
            # cannot turn an overlapping cross into an after-flat recross.
            for key, rec in self.owners.book.items():
                if (
                    rec.get("trigger_tf")
                    and self.h.pairs.get(rec["tf"]) == rec["trigger_tf"]
                    and (rec.get("intrabar_origin") or rec.get("missing_since") is None)
                    and key not in active_ids
                    and not rec.get("flat_verified_msc")
                ):
                    self._mark_flat_check(rec)
        if self.owners.prune(active_ids, time.time()):
            self.persist()
        self.ladders.prune(active)
        if stale := set(goals) - active:
            for k in stale:
                del goals[k]
            self.persist()
        self.last_service = time.monotonic()

    def finalize(
        self, name, tf, plan, res, expected, *, minute_source=None, pair_child=None, origin=None
    ):
        mt = self.mt
        if origin is not None:
            origin = dict(origin)
            origin["entry_deal"] = getattr(res, "deal", None)
        self.account()
        if (
            origin
            and res is not None
            and res.retcode in (mt.TRADE_RETCODE_DONE, 10010)
            and 0 < (getattr(res, "volume", None) or 0) < plan.lots - 1e-8
        ):
            # Do not top up or enroll a full-size ladder for a partial. Only an
            # exact opening order + deal + position can inherit the tick guard.
            self._bind_partial(name, tf, pair_child, origin, plan, res, expected)
            return None, "uncertain_or_partial_fill"
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
        return self.enroll_fill(
            name,
            tf,
            plan,
            p,
            ps,
            os,
            minute_source=minute_source,
            pair_child=pair_child,
            origin=origin,
        )

    def _bind_partial(self, name, tf, child, origin, plan, res, expected):
        order, did = getattr(res, "order", 0), getattr(res, "deal", 0)
        if not order or not did:
            return
        mt = self.mt
        deals = mt.history_deals_get(ticket=did)
        ps = mt.positions_get()
        if deals is None or len(deals) != 1 or ps is None:
            return
        z = deals[0]
        matching = [
            p
            for p in ps
            if self.owned(p)
            and p.symbol == name
            and p.ticket not in expected
            and order in (p.ticket, getattr(p, "identifier", p.ticket))
            and getattr(p, "identifier", p.ticket) == z.position_id
        ]
        if (
            len(matching) != 1
            or z.ticket != did
            or z.order != order
            or z.entry != mt.DEAL_ENTRY_IN
            or z.symbol != name
            or z.magic != self.h.magic
            or z.type != self.typ(plan.side)
            or z.time_msc < origin["ms"]
            or abs(z.volume - res.volume) > 1e-8
        ):
            return
        p = matching[0]
        if (
            p.type == z.type
            and p.price_open == z.price
            and abs(p.volume - z.volume) <= 1e-8
            and p.sl == plan.sl
            and p.tp == plan.tp
            and self.owners.lane(p) is None
            and self.owners.bind(
                p, tf, pair_child=child, origin=origin, policy_key=self.indicator_key
            )
        ):
            self.open_lanes.add((name, tf))
            self.persist()
            self.emit(
                "intrabar_partial_guarded",
                symbol=name,
                tf=tf,
                trigger_tf=child,
                ticket=p.ticket,
                trigger_msc=origin["ms"],
                volume=p.volume,
            )

    def enroll_fill(
        self,
        name,
        tf,
        plan,
        p,
        ps,
        os,
        *,
        minute_source=None,
        pair_child=None,
        origin=None,
        target_policy=TARGET_POLICY,
    ):
        mt = self.mt
        grid = Grid.from_info(mt.symbol_info(name))
        tol = grid.tick * 1e-6
        if (
            p.type != self.typ(plan.side)
            or abs(p.volume - plan.lots) > 1e-8
            or not math.isfinite(p.price_open)
            or p.price_open <= 0
        ):
            return None, "fill_not_reconciled"
        if not self.owners.bind(
            p,
            tf,
            minute_source=minute_source,
            policy_key=self.indicator_key,
            pair_child=pair_child,
            origin=origin,
        ):
            return None, "lane_identity_not_reconciled"
        self.persist()
        self.open_lanes.add((name, tf))
        if self.h.exit_policy == FIXED:
            self.state.setdefault("target_by_ticket", {})[str(p.ticket)] = plan.goal_usd
            self.persist()
        original_sl = p.sl
        if abs(original_sl - plan.sl) > tol or not (p.sl > 0 and p.tp > 0):
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
                "sl": original_sl,
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
                or verify[0].sl != original_sl
            ):
                return None, "target_alignment_unverified"
            p = verify[0]
        if self.h.exit_policy == LADDER:
            # Do not enroll a pre-existing, partial, unknown or unverified ticket.
            self.ladders.enroll(p, plan, target_policy=target_policy)
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
                **w.reading.entry_audit(),
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
        if self.h.execution_mode != d.execution_mode:
            return "execution_mode_mismatch"
        if d.paired and self.h.pairs.get(d.tf) != d.trigger_tf:
            return "pair_mapping_mismatch"  # a recognizable older tag is not an enabled trigger
        if not d.entry_side:
            return d.entry_reason
        if d.intrabar != (
            self.h.effective_timing == INTRABAR
        ) and not self._closed_fallback(d):
            return "entry_timing_mismatch"
        if d.intrabar:
            if (
                self.verify_signal is None
                or d.detected_s is None
                or self.retest
                or d.cross_bar is not None
            ):
                return "intrabar_signal_unverified"
            seen = self.state.setdefault("intrabar_reserved_ms", {})
            if (
                seen.get(name, -1) >= d.signal_msc
                or self._live_reserved(name, d)
                or self.state.get("pair_consumed", {}).get(name, -1) >= d.signal_s
                or self.state.get("consumed", {}).get(name, -1) >= d.signal_s
            ):
                return "already_consumed"
            seen[name] = d.signal_msc  # consume the episode, not the candle
            self.state.setdefault("intrabar_origin_ends", {}).setdefault(name, {})[
                f"{d.tf}:{d.candle_tf}"
            ] = d.close_s
            self.persist()
            deadline = min(d.signal_s + self.lim.max_entry_age_s, d.close_s)
            return self._attempt(
                name,
                FirstQuoteWindow(d, time.monotonic() + deadline - time.time(), raw_tf),
            )
        if (
            d.paired
            and self.state.get("intrabar_origin_ends", {})
            .get(name, {})
            .get(f"{d.tf}:{d.candle_tf}", -1)
            >= d.close_s
        ):
            return "already_consumed"
        if self.retest and (reason := self.retest.blocked(name)):
            return reason
        if d.trigger_tf is not None:
            if (
                self.verify_signal is None
                or d.anchor_s != d.close_s - d.candle_tf * 60
                or d.detected_s is None
                or d.stop_tick_size is None
            ):
                return "pair_signal_unverified" if d.paired else "m1_signal_unverified"
            if (
                self.state.get("pair_consumed" if d.paired else "m1_consumed", {}).get(
                    name, -1
                )
                >= d.close_s
            ):
                return "already_consumed"
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
        if d.trigger_tf is not None:
            self.state.setdefault("pair_consumed" if d.paired else "m1_consumed", {})[
                name
            ] = d.close_s
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

    def _attempt(self, name, window, *, force_market=False):
        d = window.reading
        window.polls += 1
        expected = set()
        n = None
        sent = 0
        recheck = None  # only an unsent request; never retry order_send
        reprices = 0
        flexible_quote = force_market or (self.h.retest_market_fallback and not d.intrabar)
        max_reprices = 10 if flexible_quote else 1
        level = BatchLevel() if d.paired and self.retest is None else None
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
                    force_market=force_market,
                )
                if level is not None:
                    level.observe(q, Grid.from_info(i), plan)
                elif recheck is None:
                    base_bid, base_ask = q.bid, q.ask
                    base_grid = Grid.from_info(i)
                    tolerance = base_grid.tick * (
                        (
                            self.h.retest_quote_drift_ticks
                            if flexible_quote
                            else 1
                        )
                        + 1e-9
                    )
                elif (
                    max(abs(q.bid - base_bid), abs(q.ask - base_ask)) > tolerance
                    or Grid.from_info(i) != base_grid
                    or (max_reprices == 1 and (q.bid, q.ask) != recheck)
                ):
                    raise Block("final_quote_or_safety_changed")
                if n is None:
                    n = left
                    if self.retest and not force_market:
                        outcome = self.retest.stage(name, window, plan, n, q, i)
                        if (
                            self.h.retest_market_fallback
                            and outcome in RETEST_MARKET_FALLBACK_REASONS
                            and not self.retest.active.get(name)
                        ):
                            self.emit(
                                "retest_market_fallback",
                                symbol=name,
                                tf=d.tf,
                                signal=d.close_s,
                                reason=outcome,
                            )
                            return self._attempt(name, window, force_market=True)
                        return outcome
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
                        stop_spread_buffer_pips=(
                            plan.stop_spread_buffer / self.syms[name][1].pip_size
                        ),
                        stop_spread_quote_price=float(quote_spread(q.bid, q.ask)),
                        goal_per_leg=plan.goal_usd,  # indicative first leg, not a promise for siblings
                        risk_batch=n * plan.risk_usd,
                        **d.clock_fields(),
                        **d.entry_audit(),
                        **(asdict(plan) | plan.stop_audit(q.bid, q.ask)),
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
                    "comment": d.order_tag(sent + 1),
                }
                chk = self.mt.order_check(req)
                a = self.account()
                retcode = getattr(chk, "retcode", None)
                quote_refusal = level is not None and retcode in (10004, 10020)  # REQUOTE / PRICE_CHANGED, order_check ONLY
                if retcode != 0:
                    self.emit(
                        "broker_precheck_rejected",
                        symbol=name,
                        tf=d.tf,
                        signal=d.signal_s,
                        leg=sent + 1,
                        retcode=retcode,
                    )
                    if not quote_refusal:
                        raise Block("broker_precheck_rejected")
                elif why := self._margin_check(
                    name, d, a, "order_check", 1, getattr(chk, "margin_free", None)
                ):
                    raise Block(why)
                # Do not send the inspected plan after a change in exposures/quote, a kill, or a stale signal.
                _, again, q2, i2 = self.prepare(
                    name,
                    d,
                    window,
                    "pre_send",
                    expected,
                    n - sent,
                    force_market=force_market,
                )
                # All routes send exactly the checked plan. The paired market route
                # binds the batch to its level rather than to a one-tick radius.
                checked_prices = (q2.bid, q2.ask)
                if level is not None:
                    level.observe(q2, Grid.from_info(i2), again)
                    if checked_prices != (q.bid, q.ask):
                        # Only this unsent leg is discarded. Re-run prepare, risk,
                        # margin and order_check for n - sent, never replenish n.
                        recheck = checked_prices
                        reprices += 1
                        continue
                    if quote_refusal:
                        raise Block("broker_precheck_rejected")
                    if again != plan:
                        raise Block("quote_or_plan_changed")  # not a tick move
                else:
                    if (
                        max(abs(q2.bid - base_bid), abs(q2.ask - base_ask)) > tolerance
                        or Grid.from_info(i2) != base_grid
                    ):
                        raise Block("quote_or_plan_changed")
                    if (
                        max_reprices == 1
                        and recheck is not None
                        and checked_prices != recheck
                    ):
                        raise Block("final_quote_or_safety_changed")
                    if again != plan:
                        # A tighter SELL translation on the same raw wick can lower
                        # risk. Rebuild only an unsent leg inside its original budget;
                        # never patch a checked request or alter an installed stop.
                        tighter_sell = (
                            plan.side == again.side == -1
                            and again.raw_chart_sl == plan.raw_chart_sl
                            and again.sl < plan.sl
                            and quote_spread(q2.bid, q2.ask)
                            < quote_spread(q.bid, q.ask)
                            and again.risk_usd <= plan.risk_usd
                        )
                        unchanged_stop = again.sl == plan.sl and (
                            flexible_quote
                            or (
                                again.risk_usd == plan.risk_usd
                                and again.entry_spread_usd != plan.entry_spread_usd
                            )
                        )
                        if (
                            reprices < max_reprices
                            and checked_prices != (q.bid, q.ask)
                            and (flexible_quote or again.entry == plan.entry)
                            and (unchanged_stop or tighter_sell)
                        ):
                            reprices += 1
                            recheck = checked_prices
                            continue
                        raise Block("quote_or_plan_changed")
                self.state["manual_hold"] = {
                    "symbol": name,
                    "tf": d.tf,
                    "signal": d.signal_s,
                    **d.clock_fields(),
                    "leg": sent + 1,
                    **(
                        {
                            "trigger_tf": d.trigger_tf,
                            (
                                "trigger_source" if d.paired else "m1_source"
                            ): d.active_source,
                            "parent_tf": d.tf,
                        }
                        if d.trigger_tf is not None
                        else {}
                    ),
                    "target_policy": TARGET_POLICY,
                    "initial_goal_usd": plan.goal_usd,
                    "entry_spread_usd": plan.entry_spread_usd,
                }
                self.persist()
                self.account()
                try:
                    q3, _ = self._entry_quote(name, window, "final_send")
                    final_prices = (q3.bid, q3.ask)
                    if self.kill.exists():
                        raise Block("final_quote_or_safety_changed")
                    if level is not None:
                        grid = Grid.from_info(self.mt.symbol_info(name))
                        level.observe(q3, grid, plan)
                        cap = self.syms[name][1].max_spread
                        if cap is not None and q3.ask - q3.bid > cap + grid.tick * 1e-7:
                            raise Block("spread_cap")
                    elif (
                        max(abs(q3.bid - base_bid), abs(q3.ask - base_ask)) > tolerance
                        or not plan.covers_spread(q3.bid, q3.ask)
                        or (reprices >= max_reprices and final_prices != checked_prices)
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
                    # Rebuild and broker-check the actual new request at this exact
                    # quote on the same signal/deadline. Never edit the price of an
                    # already checked request and send it blind.
                    reprices += 1
                    recheck = final_prices
                    self.state.pop("manual_hold")  # no send was made
                    self.persist()
                    continue
                submit = time.time()
                if d.trigger_tf is not None:
                    # Do not allow a slow precheck/persist to carry this quote across a deadline.
                    try:
                        final_check = self._quote_clock(gate, window, q3, "submit")
                        gate.require("submit", decision=final_check.checked)
                        if self.syms[name][1].session == "configured" and (
                            why := self.hours.status(final_check.checked)[0]
                        ):
                            raise Block(why)
                        if (
                            final_check.checked >= d.close_s + (0 if d.intrabar else d.candle_tf * 60)
                            or self.kill.exists()
                            or (
                                self.pair_fence(name, d.tf, d.trigger_tf, -d.label)
                                >= d.signal_msc
                                if d.paired
                                else self.minute_invalidated(name, d)
                            )
                        ):
                            raise Block("final_quote_or_safety_changed")
                    except (Block, EntrySessionVeto):
                        self.state.pop("manual_hold")
                        self.persist()
                        raise
                    submit = final_check.checked
                start_call = time.monotonic()
                res = self.mt.order_send(req)
                call_ms = (time.monotonic() - start_call) * 1000
                self.emit(
                    "demo_result",
                    symbol=name,
                    tf=d.tf,
                    stop_tick_size=d.stop_tick_size,
                    cross_atr=d.atr,
                    leg=sent + 1,
                    signal=d.signal_s,
                    request=req,
                    **d.clock_fields(),
                    **d.entry_audit(),
                    **(
                        {
                            ("trigger_to_submit_ms" if d.intrabar else "close_to_submit_ms"): round((submit - d.signal_s) * 1000, 3),
                            "order_send_roundtrip_ms": round(call_ms, 3),
                            "submit_utc": utc(submit),
                        }
                        if d.trigger_tf is not None
                        else {}
                    ),
                    **(
                        {
                            "max_entry_age_s": self.lim.max_entry_age_s,
                            "max_quote_age_s": self.lim.max_quote_age_s,
                        }
                        if d.paired
                        else {}
                    ),
                    unsent_reprices=reprices,
                    send_check_utc=utc(checked),
                    entry_session=self.session_gate(name).report(checked),
                    send_tick_time_msc=int(q3.time_msc),
                    send_bid=q3.bid,
                    send_ask=q3.ask,
                    target_policy=TARGET_POLICY,
                    **plan.stop_audit(q.bid, q.ask),
                    stop_spread_buffer_pips=(
                        plan.stop_spread_buffer / self.syms[name][1].pip_size
                    ),
                    planned_stop_spread_quote_price=float(quote_spread(q.bid, q.ask)),
                    stop_spread_quote_price=float(quote_spread(q3.bid, q3.ask)),
                    configured_target_usd=plan.configured_target_usd,
                    entry_spread_usd=plan.entry_spread_usd,
                    initial_goal_usd=plan.goal_usd,
                    planned_tp_profit_usd=plan.reward_usd,
                    retcode=getattr(res, "retcode", None),
                    **({"opening_order": getattr(res, "order", None), "fill_volume": getattr(res, "volume", None)} if d.intrabar else {}),
                    price=getattr(res, "price", None),
                    deal=getattr(res, "deal", None),
                )
                ticket, status = self.finalize(
                    name,
                    d.tf,
                    plan,
                    res,
                    expected,
                    minute_source=d.active_source if d.trigger_tf == 1 else None,
                    pair_child=d.trigger_tf if d.paired else None,
                    origin=d.guard(plan) if d.intrabar else None,
                )
                # A synchronous send/ack can itself span an uncross. Sample
                # after enrollment before another leg, including a verified
                # partial; unknown acknowledgments never authorize a re-send.
                intact = not d.intrabar or self.verify_signal(name, d, self.mt.symbol_info_tick(name))
                if ticket is None:
                    return status + "_manual_hold"
                expected.add(ticket)
                sent += 1
                recheck = None
                reprices = 0
                self.state.pop("manual_hold")
                self.persist()
                if sent == n:
                    self.service_if_due()
                    if self.watch_other_symbols is not None:
                        self.watch_other_symbols(name)
                    return "demo_batch_sent"
                if not intact:
                    raise Block("live_cross_or_context_unverified")
                if d.intrabar and self.watch_other_symbols is not None:
                    self.watch_other_symbols(name)
        except TimingBlock as exc:
            check = exc.check
            wait = (
                not d.intrabar
                and (
                    exc.stage == "first_plan"
                    or (exc.stage == "opposite_flat_wait" and self._closed_fallback(d))
                )
                and n is None
                and not sent
                and check.waitable
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
                    **({"trigger_tf": d.candle_tf} if d.paired else {}),
                    **check.audit(d.candle_tf, live=d if d.intrabar else None),
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
        except AwaitOwnedFlat as exc:
            # A reservation and its original wall + monotonic deadline already exist.
            # Do not construct a new window or resume after process restart.
            why = str(exc)
            if n is None and not sent and self._closed_fallback(d):
                if window.last_wait_reason != why:
                    self.emit(
                        "pair_wait_opposite_flat",
                        symbol=name,
                        tf=d.tf,
                        trigger_tf=d.trigger_tf,
                        close_s=d.close_s,
                        deadline_s=d.signal_s + self.lim.max_entry_age_s,
                    )
                window.last_wait_reason = why
                self.waiting[(name, d.tf)] = window
                return WAITING
        except Block as exc:
            why = str(exc)
            # No order exists. Only a positive, insufficient placement gap may wait;
            # an observed touch, a failed broker request or any other veto is terminal.
            if (
                self.retest
                and not self.h.retest_market_fallback
                and n is None
                and why == "retest_placement_gap"
                and time.monotonic() < window.end_mono
                and time.time() < d.signal_s + self.lim.max_entry_age_s
            ):
                if window.last_wait_reason != why:
                    self.emit(
                        "retest_wait_for_legal_gap",
                        symbol=name,
                        tf=d.tf,
                        signal=d.close_s,
                    )
                window.last_wait_reason = why
                self.waiting[(name, d.tf)] = window
                return WAITING
            if (
                self.h.retest_market_fallback
                and self.retest
                and not force_market
                and sent == 0
                and why in RETEST_MARKET_FALLBACK_REASONS
                and not self.retest.active.get(name)
            ):
                self.emit(
                    "retest_market_fallback",
                    symbol=name,
                    tf=d.tf,
                    signal=d.close_s,
                    reason=why,
                )
                return self._attempt(name, window, force_market=True)
        if why == "manual_reconciliation_required":
            return why
        return (
            "manual_hold:"
            if self.state.get("manual_hold")
            else "protected_partial_no_topup:"
            if sent
            else ""
        ) + why
