"""Separate Windows MT5 runner. The website is not an order API."""

from __future__ import annotations

import hashlib
import itertools
import json
import time
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from bot.broker import DemoSafetyError, _account_order_lock, fresh, snapshot
from bot.config.entry_sessions import EntrySessionVeto, sessions_for_runner
from bot.hull.orders import Block, Grid

from .engine import TF, Bar, Engine, ticks
from .execution import ENTRY_SECONDS, MAGIC, Intents, execute, fill_type, identity

HISTORY = 1800


class Stream:
    def __init__(self, mt, pair, rules):
        self.mt, self.pair, self.rules = mt, pair, rules
        info = mt.symbol_info(pair.symbol)
        if (
            info is None
            or info.name != pair.symbol
            or not mt.symbol_select(pair.symbol, True)
        ):
            raise DemoSafetyError(
                f"{pair.symbol}: use the exact Market Watch name, including its suffix"
            )
        if getattr(info, "chart_mode", None) != getattr(mt, "SYMBOL_CHART_MODE_BID", 0):
            raise DemoSafetyError(
                f"{pair.symbol}: expected bid-chart bars; last-price charts are unsupported"
            )
        self.grid = Grid.from_info(info)
        self.grid.volume(pair.lots)
        fill_type(mt, info)
        self.width = pair.zone_ticks(rules, self.grid.tick)
        self.eng = Engine(rules, self.width)
        self.wait = None
        self.needs_warm = True

    def read(self, n, now):
        # MT5 position 0 is forming. Never request it or synthesize its closing price.
        rs = self.mt.copy_rates_from_pos(self.pair.symbol, self.mt.TIMEFRAME_M15, 1, n)
        if rs is None or len(rs) < n:
            raise ValueError("insufficient completed M15 broker history")
        bars = [
            Bar(
                int(r["time"]),
                *[
                    ticks(r[x], self.grid.tick)
                    for x in ("open", "high", "low", "close")
                ],
            )
            for r in rs
        ]
        if bars[-1].t + TF > now or any(
            x.t >= y.t for x, y in itertools.pairwise(bars)
        ):
            raise ValueError("invalid order or forming M15 in history")
        return bars

    def refresh(self, now):
        try:
            if not self.needs_warm:
                bars = self.read(2, now)
                if bars[-1].t == self.eng.last:
                    return None, "unchanged"
                if bars[0].t == self.eng.last and bars[1].t == bars[0].t + TF:
                    return self.eng.update(bars[-1]), "closed"
            self.wait = None
            bars = self.read(HISTORY, now)
            self.eng = Engine(self.rules, self.width)
            for b in bars:
                self.eng.update(b)
            self.eng.pending.clear()  # structure only; never recover watches/entries after downtime
            self.needs_warm = False
            return None, "warmup_only"
        except (ValueError, ArithmeticError, TypeError, KeyError) as exc:
            self.wait = None
            self.eng.pending.clear()
            self.needs_warm = True
            return None, "history_unavailable: " + str(exc)


def protected_lock(stack, root, key):
    # Exact same account locks used by current Hull and old zone scanners. The
    # Wickless magic, ledger and logs are separate; don't run both armed together.
    stack.enter_context(_account_order_lock(root / "hull" / key / "scanner.lock", 1))
    stack.enter_context(
        _account_order_lock(Path("reports/zone_runtime") / key / "scanner.lock", 1)
    )


def run(cfg, action, *, arm=False, terminal_path="", review_flat=False):
    armed = action == "trade"
    if armed and (not arm or cfg.mode != "demo" or not cfg.server):
        raise DemoSafetyError(
            "Sending requires a private mode: demo config, exact server and --arm-demo"
        )
    if action == "reconcile" and (not review_flat or cfg.mode != "demo"):
        raise DemoSafetyError(
            "Reconcile only after reviewing the MT5 deals and verifying the account is flat"
        )
    try:
        import MetaTrader5 as mt
    except ImportError as exc:
        raise DemoSafetyError(
            "Run terminal commands on the Windows MT5 PC: install MetaTrader5 tzdata"
        ) from exc
    if not (mt.initialize(terminal_path) if terminal_path else mt.initialize()):
        raise DemoSafetyError(f"MT5 initialization failed: {mt.last_error()}")
    try:
        ident = identity(mt, cfg, armed)
        key = hashlib.sha256(repr(ident).encode()).hexdigest()[:16]
        home = Path.home() / ".moneyprinterbot"
        mode = "demo" if armed else "shadow"
        root = Path("reports/wickless_runtime") / key

        def emit(event, **kw):
            data = json.dumps(
                {"utc": datetime.now(UTC).isoformat(), "event": event, **kw},
                allow_nan=False,
            )
            print(data, flush=True)
            if action in ("observe", "trade"):
                root.mkdir(exist_ok=True, parents=True)
                with (root / (mode + ".jsonl")).open("a", encoding="utf8") as f:
                    print(data, file=f)

        with ExitStack() as stack:
            book = None
            if armed or action == "reconcile":
                protected_lock(stack, home, key)
                book = Intents(home / "wickless" / key / "intents.sqlite3")
                stack.callback(book.close)
                ps, os = snapshot(mt)
                if ps or os:
                    raise DemoSafetyError(
                        "Review existing positions/orders first: armed startup and reconcile require a flat account"
                    )
                if action == "reconcile":
                    identity(mt, cfg, False, ident)
                    for sym, stamp, detail in book.unresolved():
                        book.mark(sym, stamp, "manually_reviewed_flat", detail)
                        emit("acknowledged_flat_no_order", symbol=sym, close_s=stamp)
                    return
                if book.unresolved():
                    raise DemoSafetyError(
                        "Unresolved submission. Review MT5 and see the reconcile procedure in docs/WICKLESS.md"
                    )
            sessions = sessions_for_runner()  # one immutable snapshot for this process
            lines = [Stream(mt, p, cfg.rules) for p in cfg.pairs]
            ps, os = snapshot(mt)
            emit(
                "startup",
                operation=action,
                broker=cfg.broker,
                server=ident[1],
                account_hash=key,
                magic=MAGIC,
                symbols=[p.symbol for p in cfg.pairs],
                existing_positions=len(ps),
                existing_orders=len(os),
                timeframe="M15",
                entry="rejection close",
                entry_sessions=sessions.identity(),
                target="1:1 price distance",
                sends_orders=armed,
            )
            issues = []
            for lane in lines:
                name = lane.pair.symbol
                gate = sessions.bind(name)
                if gate.issue:
                    issues.append(f"{name}: {gate.issue}; no new entries")
                _, status = lane.refresh(time.time())
                q = mt.symbol_info_tick(name)
                quote_ok = fresh(q, time.time(), 4)
                spread = (
                    (q.ask - q.bid) / float(lane.pair.pip(lane.grid.tick))
                    if quote_ok
                    else None
                )
                last_close = lane.eng.last + TF if lane.eng.last is not None else None
                info = mt.symbol_info(name)
                if info is None or info.trade_mode in (
                    mt.SYMBOL_TRADE_MODE_DISABLED,
                    mt.SYMBOL_TRADE_MODE_CLOSEONLY,
                ):
                    issues.append(name + ": symbol not open for new trades")
                if (
                    status != "warmup_only"
                    or last_close is None
                    or not 0 <= time.time() - last_close <= TF + ENTRY_SECONDS
                    or not quote_ok
                    or spread > lane.pair.max_spread_pips
                ):
                    issues.append(name + ": history/quote/spread not ready")
                emit(
                    "symbol_check",
                    symbol=name,
                    history=status,
                    entry_session=gate.report(time.time()),
                    last_closed_utc_s=last_close,
                    trend=lane.eng.structure.bias,
                    lots=lane.pair.lots,
                    tick_size=lane.grid.tick,
                    zone_ticks=lane.width,
                    spread_pips=spread,
                    risk_cap_usd=lane.pair.max_risk_usd,
                )
            if action == "preflight" or armed:
                a, term = mt.account_info(), mt.terminal_info()
                if (
                    not a.trade_allowed
                    or not a.trade_expert
                    or not term.trade_allowed
                    or getattr(term, "tradeapi_disabled", False)
                ):
                    issues.append("MT5 trade permissions unavailable")
                if ps or os or Path("KILL").exists():
                    issues.append("Existing exposure or KILL file; review before demo")
                if issues:
                    raise DemoSafetyError("; ".join(issues))
                emit("preflight_passed")
                if action == "preflight":
                    return
            last_status = {}
            while True:
                identity(mt, cfg, armed, ident)
                for lane in lines:
                    p = lane.pair
                    out, status = lane.refresh(time.time())
                    if status != "unchanged" and (
                        out or last_status.get(p.symbol) != status
                    ):
                        if out and (out.raw or out.event or out.ended):
                            detail = asdict(out)
                            detail["structure_event"] = detail.pop("event")
                            emit(
                                "m15_close",
                                symbol=p.symbol,
                                close_s=lane.eng.last + TF,
                                **detail,
                            )
                        elif not out:
                            emit("stream", symbol=p.symbol, status=status)
                        last_status[p.symbol] = status
                    if out:
                        lane.wait = out.signal
                    if lane.wait:
                        try:
                            result = execute(
                                mt,
                                cfg,
                                p,
                                lane.grid,
                                lane.wait,
                                ident,
                                armed=armed,
                                book=book,
                                now=time.time,
                                sessions=sessions,
                            )
                        except EntrySessionVeto as exc:
                            result = {"status": str(exc), **exc.fields}
                        except Block as exc:
                            if (
                                str(exc) == "waiting_post_close_quote"
                                and time.time() < lane.wait.close_s + ENTRY_SECONDS
                            ):
                                continue
                            result = {"status": str(exc)}
                        emit(
                            "entry_decision",
                            symbol=p.symbol,
                            close_s=lane.wait.close_s,
                            setup_open_s=lane.wait.zone.born,
                            signal_session=sessions.bind(p.symbol).report(
                                lane.wait.close_s
                            ),
                            side="BUY" if lane.wait.zone.side == 1 else "SELL",
                            rejection_close=lane.grid.price(lane.wait.entry),
                            **result,
                        )
                        lane.wait = None
                time.sleep(0.5)
    finally:
        mt.shutdown()
