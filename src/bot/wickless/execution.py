"""One protected market deal per admitted rejection; no limits, retries, batches or ladder."""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path

from bot.config.entry_sessions import EntrySessionVeto, sessions_for_runner
from bot.execution.demo_runner import DemoSafetyError, _side_is_allowed
from bot.hull.desk import identity_check
from bot.hull.orders import Block, Grid
from bot.zones.monitor import fresh, snapshot

from .engine import Signal, ticks

MAGIC = 26111571
ENTRY_SECONDS, QUOTE_SECONDS = 8, 4


def identity(mt, cfg, armed, ident=None):
    a = identity_check(mt, cfg.mode, armed, ident)
    if cfg.server and a.server != cfg.server:
        raise DemoSafetyError("Wrong MT5 server for this broker profile")
    if armed and (
        not cfg.server or getattr(mt.terminal_info(), "tradeapi_disabled", False)
    ):
        raise DemoSafetyError("Pin the demo server and enable the local trading API")
    return int(a.login), str(a.server)


class Intents:
    """SQLite autocommits BEFORE broker I/O. A crash/uncertain send blocks the next run."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS intents(symbol TEXT NOT NULL, close_s INTEGER NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL, PRIMARY KEY(symbol,close_s))"
        )

    def unresolved(self):
        return self.db.execute(
            "SELECT symbol,close_s,detail FROM intents WHERE state='review'"
        ).fetchall()

    def reserve(self, symbol, stamp):
        if self.unresolved():
            raise DemoSafetyError(
                "Unresolved submission: inspect the MT5 history before continuing"
            )
        try:
            self.db.execute(
                "INSERT INTO intents VALUES(?,?,'review','submission reserved; result not verified')",
                (symbol, stamp),
            )
        except sqlite3.IntegrityError as exc:
            raise Block("already_consumed") from exc

    def mark(self, symbol, stamp, state, detail):
        self.db.execute(
            "UPDATE intents SET state=?,detail=? WHERE symbol=? AND close_s=?",
            (state, detail, symbol, stamp),
        )

    def close(self):
        self.db.close()


def fill_type(mt, info):
    mode = int(info.filling_mode)
    if mode & getattr(mt, "SYMBOL_FILLING_FOK", 1):
        return mt.ORDER_FILLING_FOK
    if mode & getattr(mt, "SYMBOL_FILLING_IOC", 2):
        return mt.ORDER_FILLING_IOC
    raise Block("no_FOK_or_IOC_market_filling_policy")


def tick_at(mt, sym, stamp, now):
    if not stamp <= now <= stamp + ENTRY_SECONDS:
        raise Block("outside_entry_clock")
    q = mt.symbol_info_tick(sym)
    if not fresh(q, now, QUOTE_SECONDS) or q.time_msc < stamp * 1000:
        raise Block("waiting_post_close_quote")
    return q


def protection(g, d, entry, sl, bid, ask):
    tp = 2 * entry - sl
    mark = bid if d == 1 else ask
    gap = max(g.gap, g.tick)
    if (
        min(sl, tp) <= 0
        or min(d * (mark - sl), d * (tp - mark)) * g.tick + g.tick * 1e-8 < gap
        or d * (entry - sl) <= 0
    ):
        raise Block("illegal_bracket_no_widening")
    return tp


def risk(mt, typ, symbol, lots, entry, sl, cap):
    money = mt.order_calc_profit(typ, symbol, lots, entry, sl)
    if money is None or not math.isfinite(money) or money >= 0:
        raise Block("risk_valuation_unavailable")
    if -money > cap + 1e-8:
        raise Block("risk_cap_no_resize")
    return -money


def exposure(mt, cfg, symbol):
    ps, os = snapshot(mt)
    if os or any(
        p.magic != MAGIC
        or p.symbol not in {x.symbol for x in cfg.pairs}
        or not all(math.isfinite(v) and v > 0 for v in (p.sl, p.tp))
        for p in ps
    ):
        raise Block("foreign_or_unprotected_exposure")
    if any(p.symbol == symbol for p in ps):
        raise Block("pair_occupied")
    if len(ps) >= cfg.max_positions:
        raise Block("position_cap")


def legal_check(mt, req):
    v = mt.order_check(req)
    if v is None or v.retcode != 0:
        raise Block("broker_order_check_rejected")
    if "volume" in req and (not math.isfinite(v.margin_free) or v.margin_free < 0):
        raise Block("insufficient_free_margin")


def checked_position(mt, pair, res, typ):
    ps, _ = snapshot(mt)
    found = [
        p for p in ps if p.symbol == pair.symbol and p.magic == MAGIC and p.type == typ
    ]
    if len(found) != 1:
        raise DemoSafetyError(
            "Fill not uniquely visible; inspect deals (it may already be closed)"
        )
    p = found[0]
    # On a newly opened hedging position, the opening order is its ticket. Some
    # servers identify it only via the deal's position identifier.
    if not getattr(res, "order", 0) or res.order != p.ticket:
        pos_id = getattr(p, "identifier", p.ticket)
        ds = mt.history_deals_get(position=pos_id) if getattr(res, "deal", 0) else None
        if ds is None or not any(
            x.ticket == res.deal
            and x.position_id == getattr(p, "identifier", p.ticket)
            and x.magic == MAGIC
            and x.symbol == pair.symbol
            for x in ds
        ):
            raise DemoSafetyError("Cannot link fill to this exact position")
    if not all(
        math.isfinite(v) and abs(v - pair.lots) <= 1e-8 for v in (p.volume, res.volume)
    ):
        raise DemoSafetyError("Partial/ambiguous filled volume")
    return p


def execute(
    mt,
    cfg,
    pair,
    initial_grid,
    sig: Signal,
    ident,
    *,
    armed,
    book,
    now,
    kill=Path("KILL"),
    sessions=None,
):
    """Poll only on a pre-close/stale quote; all other vetoes consume this signal."""
    identity(mt, cfg, armed, ident)
    if kill.exists():
        raise Block("KILL_present")
    if pair not in cfg.pairs:
        raise Block("symbol_not_selected")
    z = sig.zone
    sym, d = pair.symbol, z.side
    info = mt.symbol_info(sym)
    g = Grid.from_info(info)
    if g != initial_grid or info.name != sym:
        raise Block("symbol_spec_changed_restart")
    g.volume(pair.lots)
    if not _side_is_allowed(mt, info, "BUY" if d == 1 else "SELL"):
        raise Block("symbol_side_disabled")
    exposure(mt, cfg, sym)
    sessions = sessions if sessions is not None else sessions_for_runner()
    gate = sessions.bind(sym)
    checked = now()
    # A retest may start out of hours. Only its rejection CLOSE is an entry signal.
    gate.require("first_quote", rejection_close=sig.close_s, decision=checked)
    q = tick_at(mt, sym, sig.close_s, checked)
    gate.require("first_quote", quote=q.time_msc / 1000)
    try:
        bid, ask = ticks(q.bid, g.tick), ticks(q.ask, g.tick)
    except ValueError as e:
        raise Block("invalid_quote_grid") from e
    if (ask - bid) * g.tick > pair.max_spread_pips * float(
        pair.pip(g.tick)
    ) + g.tick * 1e-7:
        raise Block("spread_cap")
    if (d == 1 and bid < z.lo) or (d == -1 and bid > z.hi):
        raise Block("quote_through_base")
    entry, typ = (ask, mt.ORDER_TYPE_BUY) if d == 1 else (bid, mt.ORDER_TYPE_SELL)
    sl = z.sl
    tp = protection(g, d, entry, sl, bid, ask)
    estimated = risk(
        mt, typ, sym, pair.lots, g.price(entry), g.price(sl), pair.max_risk_usd
    )
    req = {
        "action": mt.TRADE_ACTION_DEAL,
        "symbol": sym,
        "volume": pair.lots,
        "type": typ,
        "price": g.price(entry),
        "sl": g.price(sl),
        "tp": g.price(tp),
        "deviation": 5,
        "magic": MAGIC,
        "comment": f"W15:{sig.close_s}",
        "type_time": mt.ORDER_TIME_GTC,
        "type_filling": fill_type(mt, info),
    }
    fields = {
        "entry": req["price"],
        "sl": req["sl"],
        "tp": req["tp"],
        "risk_usd": estimated,
        "lots": pair.lots,
    }
    if not armed:
        checked = now()
        gate.require("observer_plan", decision=checked)
        if not sig.close_s <= checked <= sig.close_s + ENTRY_SECONDS or not fresh(
            q, checked, QUOTE_SECONDS
        ):
            raise Block("send_window_closed")
        return {
            "status": "observer_plan",
            "entry_session": gate.report(checked),
            **fields,
        }  # never invoke order_send, even for SL/TP
    if book is None or cfg.mode != "demo":
        raise DemoSafetyError("No private demo intent store or not in demo mode")
    legal_check(mt, req)
    identity(mt, cfg, True, ident)
    exposure(mt, cfg, sym)

    def final_quote(stage):
        latest = mt.symbol_info_tick(sym)
        killed = kill.exists()
        clock = now()
        gate.require(stage, decision=clock)
        if (
            killed
            or not sig.close_s <= clock <= sig.close_s + ENTRY_SECONDS
            or not fresh(q, clock, QUOTE_SECONDS)
        ):
            raise Block("send_window_closed")
        if (
            not fresh(latest, clock, QUOTE_SECONDS)
            or latest.time_msc < q.time_msc
            or (latest.bid, latest.ask) != (q.bid, q.ask)
        ):
            raise Block("quote_changed_during_precheck_no_chase")
        gate.require(stage, quote=latest.time_msc / 1000)
        return clock

    final_quote("post_precheck")
    book.reserve(sym, sig.close_s)
    try:
        # SQLite acquisition/fsync may itself have crossed a session boundary.
        checked = final_quote("final_send")
    except (EntrySessionVeto, Block) as exc:
        book.mark(sym, sig.close_s, "not_sent", str(exc))
        raise
    try:
        res = mt.order_send(req)  # exactly one entry attempt
        if res is None or res.retcode != mt.TRADE_RETCODE_DONE:
            raise DemoSafetyError(
                f"Unverified order result: {getattr(res, 'retcode', None)}; no retry"
            )
        identity(mt, cfg, True, ident)
        p = checked_position(mt, pair, res, typ)
        fp = ticks(p.price_open, g.tick)
        if ticks(p.sl, g.tick) != sl or ticks(p.tp, g.tick) != tp:
            raise DemoSafetyError(
                "Initial broker protection differs from the sent bracket"
            )
        actual_risk = risk(
            mt, typ, sym, pair.lots, p.price_open, p.sl, pair.max_risk_usd
        )
        corrected = 2 * fp - sl
        if corrected != tp:
            current = mt.symbol_info_tick(sym)
            if not fresh(current, now(), QUOTE_SECONDS):
                raise DemoSafetyError("Cannot reprice TP on a stale quote")
            protection(
                g, d, fp, sl, ticks(current.bid, g.tick), ticks(current.ask, g.tick)
            )
            amend = {
                "action": mt.TRADE_ACTION_SLTP,
                "symbol": sym,
                "position": p.ticket,
                "magic": MAGIC,
                "sl": req["sl"],
                "tp": g.price(corrected),
            }
            legal_check(mt, amend)
            identity(mt, cfg, True, ident)
            ack = mt.order_send(
                amend
            )  # exact ticket, unchanged structural stop, TP only
            if ack is None or ack.retcode != mt.TRADE_RETCODE_DONE:
                raise DemoSafetyError("Broker did not confirm the TP correction")
            after = checked_position(mt, pair, res, typ)
            if (
                after.ticket != p.ticket
                or ticks(after.tp, g.tick) != corrected
                or ticks(after.sl, g.tick) != sl
            ):
                raise DemoSafetyError("Corrected bracket not visible on this position")
        book.mark(sym, sig.close_s, "verified", str(p.ticket))
        return {
            "status": "demo_fill_verified",
            "entry_session": gate.report(checked),
            "ticket": p.ticket,
            **fields,
            "quoted_entry": fields["entry"],
            "entry": p.price_open,
            "tp": g.price(corrected),
            "risk_usd": actual_risk,
        }
    except Exception as exc:
        book.mark(sym, sig.close_s, "review", str(exc))
        raise DemoSafetyError(
            f"{sym}: stop scanning; reconcile MT5 position/history. {exc}"
        ) from exc
