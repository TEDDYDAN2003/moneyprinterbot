"""Single Python companion for the user's running Windows MT5 Desktop terminal."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from bot.execution.demo_runner import DemoSafetyError, _account_order_lock
from bot.zones.engine import Bar

from .config import COUNTS, FRAMES
from .desk import Desk, identity_check, resolve
from .orders import Grid, choose
from .signal import Hull


def describe(r):
    return {
        **asdict(r),
        "direction": "BUY" if r.label == 1 else "SELL" if r.label == -1 else "NONE",
        "signal_close_utc": datetime.fromtimestamp(r.close_s, UTC).isoformat(),
        "anchor_bar_utc": datetime.fromtimestamp(r.anchor_s, UTC).isoformat()
        if r.anchor_s is not None
        else None,
    }


class Stream:
    def __init__(self, tf, cfg, pip):
        self.tf, self.cfg, self.pip = tf, cfg, pip
        self.h = Hull(tf, cfg, pip)
        self.last_label = None
        self.min_bars = max(
            cfg.hull_len + int(cfg.hull_len**0.5) + 2,
            cfg.atr_length + 1,
            2 * cfg.swing_len + 2,
            cfg.pullback_bars + 1,
            5 * cfg.ema_length if cfg.signal == "ema_plan" else 0,
        )

    def refresh(self, mt, name, now):
        # Native M2 bars: no M3/M15 substitution; index 0 is never supplied to the indicator.
        period = getattr(mt, f"TIMEFRAME_M{self.tf}")
        cold = self.h.prev is None
        reload = cold or now - self.h.prev.t > self.tf * 60 * 4
        limit = max(self.min_bars + 2, 1200) if reload else 4
        rates = mt.copy_rates_from_pos(name, period, 1, limit)
        if rates is None or len(rates) == 0 or (reload and len(rates) < self.min_bars):
            return None, "history_unavailable"
        if reload:
            self.h = Hull(self.tf, self.cfg, self.pip)
            self.last_label = None
        decision = None
        last = -1
        for r in rates:
            t = int(r["time"])
            if t <= last:
                raise DemoSafetyError(f"{name} M{self.tf}: unordered terminal bars")
            last = t
            if t + self.tf * 60 > now or (self.h.prev and t <= self.h.prev.t):
                continue
            decision = self.h.update(
                Bar(t, *[float(r[k]) for k in ("open", "high", "low", "close")])
            )
            if decision.label:
                self.last_label = decision
        # Reconnect/initial history may contain old labels. Never enter from warm-up.
        return (
            (None, "warmup_only")
            if reload
            else (decision, "updated" if decision else "unchanged")
        )


def sync_deals(d, now):
    # Terminal is authoritative for closes; report actual fees separately from the cash target.
    mt = d.mt
    since = d.state.get("deal_cursor", int(now) - 86400)
    known = d.state.get("seen_deals", {})
    floor = int(now) - 300
    deals = mt.history_deals_get(
        datetime.fromtimestamp(since, UTC), datetime.fromtimestamp(now + 1, UTC)
    )
    if deals is None:
        d.emit("history_unavailable")
        return False
    for x in deals:
        if d.owned(x) and str(x.ticket) not in known:
            d.emit(
                "broker_deal",
                **{
                    k: getattr(x, k, None)
                    for k in (
                        "symbol",
                        "ticket",
                        "position_id",
                        "entry",
                        "type",
                        "volume",
                        "price",
                        "time_msc",
                        "profit",
                        "commission",
                        "swap",
                        "fee",
                    )
                },
            )
            known[str(x.ticket)] = int(x.time)
    d.state.update(
        deal_cursor=floor, seen_deals={k: v for k, v in known.items() if v >= floor}
    )
    d.persist()
    return True


def run(
    cfg,
    h,
    profiles,
    frames=FRAMES,
    armed=False,
    preflight=False,
    poll=1.0,
    iterations=None,
):
    if (
        not frames
        or len(set(frames)) != len(frames)
        or set(frames) - set(FRAMES)
        or not 0.25 <= poll <= 10
        or (iterations is not None and iterations < 1)
    ):
        raise ValueError("Invalid timeframes/poll/iterations")
    mode = cfg["run"]["mode"]
    if armed and (mode != "demo" or preflight):
        raise DemoSafetyError(
            "Sending requires explicit run.mode: demo and --arm-demo; preflight is always read-only"
        )
    if mode not in ("paper", "demo"):
        raise DemoSafetyError(
            "Hull companion supports paper observation or verified DEMO only"
        )
    try:
        import MetaTrader5 as mt
    except ImportError as exc:
        raise DemoSafetyError(
            "Run on the Windows MT5 host: python -m pip install MetaTrader5 tzdata"
        ) from exc
    terminal_path = cfg.get("mt5", {}).get("terminal_path", "")
    if not (mt.initialize(terminal_path) if terminal_path else mt.initialize()):
        raise DemoSafetyError(f"MT5 initialize failed: {mt.last_error()}")
    try:
        a = identity_check(mt, mode, armed)
        identity = (int(a.login), str(a.server))
        key = hashlib.sha256(repr(identity).encode()).hexdigest()[:16]
        syms = resolve(mt, profiles)
        limits = h.limits
        specs = {}
        for name, (code, p) in syms.items():
            i = mt.symbol_info(name)
            g = Grid.from_info(i)
            q = mt.symbol_info_tick(name)
            specs[name] = {
                "key": code,
                **p.model_dump(),
                **asdict(g),
                "contract_size": i.trade_contract_size,
                "profit_currency": i.currency_profit,
                "broker_trade_mode": i.trade_mode,
                "legs_when_room": COUNTS[p.rate],
                "batch_lots_range": [
                    round(p.lots * min(COUNTS[p.rate]), 8),
                    round(p.lots * max(COUNTS[p.rate]), 8),
                ],
                "batch_target_range_usd": [
                    min(COUNTS[p.rate]) * p.target_usd,
                    max(COUNTS[p.rate]) * p.target_usd,
                ],
                "latest_spread": None if q is None else q.ask - q.bid,
            }
        summary = {
            "mode": "DEMO ARMED" if armed else "READ ONLY",
            "account": "USD hedging demo",
            "terminal_trade_allowed": bool(mt.terminal_info().trade_allowed),
            "account_expert_allowed": bool(a.trade_expert and a.trade_allowed),
            "timeframes": list(frames),
            "specs": specs,
            "limits": limits.model_dump(),
            "indicator": h.indicator.model_dump(),
        }
        if preflight:
            from bot.zones.monitor import snapshot

            ps, os = snapshot(mt)
            summary["open_positions"] = len(ps)
            summary["pending_orders"] = len(os)
            summary["history_check"] = {}
            issues = []
            for name, (_, p) in syms.items():
                checks = {}
                for tf in frames:
                    lane = Stream(tf, h.indicator, p.pip_size)
                    _, status = lane.refresh(mt, name, time.time())
                    v = lane.last_label
                    checks[f"M{tf}"] = {
                        "status": status,
                        "closed_bars_loaded": lane.h.n,
                        "last_label_for_comparison_only": describe(v) if v else None,
                    }
                    if status == "history_unavailable" or lane.h.n < lane.min_bars:
                        issues.append(f"{name} M{tf}: insufficient closed-bar history")
                summary["history_check"][name] = checks
            if ps or os:
                issues.append(
                    "Preflight found existing exposure; review before starting or changing profiles"
                )
            summary["review_before_demo"] = issues
        print(json.dumps(summary, indent=2, allow_nan=False), flush=True)
        if preflight:
            return summary
        root = Path("reports/hull_runtime") / key
        root.mkdir(parents=True, exist_ok=True)

        def emit(event, **fields):
            line = json.dumps(
                {"at": datetime.now(UTC).isoformat(), "event": event, **fields},
                allow_nan=False,
            )
            with (root / ("demo.jsonl" if armed else "shadow.jsonl")).open(
                "a", encoding="utf8"
            ) as f:
                print(line, file=f)
            print(line, flush=True)

        private = Path.home() / ".moneyprinterbot" / "hull" / key
        path = private / "state.json"
        with ExitStack() as locks:
            if armed:
                locks.enter_context(
                    _account_order_lock(private / "scanner.lock", 1)
                )  # shared even across repo copies
                locks.enter_context(
                    _account_order_lock(
                        Path("reports/zone_runtime") / key / "scanner.lock", 1
                    )
                )  # coexistence with old checkout in this directory
            state = json.loads(path.read_text()) if armed and path.exists() else {}
            d = Desk(mt, cfg, h, syms, identity, state, path, emit, armed)
            streams = {
                (name, tf): Stream(tf, h.indicator, p.pip_size)
                for name, (_, p) in syms.items()
                for tf in frames
            }
            num = 0
            sync = 0.0
            history_ok = not armed
            emit("started", summary=summary)
            while iterations is None or num < iterations:
                num += 1
                now = time.time()
                d.mark(d.account(), now)
                d.supervise()
                if armed and now - sync >= 15:
                    history_ok = sync_deals(d, now)
                    sync = now
                for name in syms:
                    ds = []
                    data_ok = True
                    for tf in frames:
                        reading, why = streams[name, tf].refresh(mt, name, time.time())
                        if why == "history_unavailable":
                            data_ok = False
                            emit("data_unavailable", symbol=name, tf=tf)
                        if reading and reading.label:
                            ds.append(reading)
                            emit("closed_label", symbol=name, **describe(reading))
                    if ds:
                        picked, why = choose(ds)
                        if not data_ok or not history_ok:
                            why = "data_or_deal_history_unavailable"
                        elif picked:
                            why = d.batch(name, picked)
                        emit(
                            "label_decision",
                            symbol=name,
                            tf=picked.tf if picked else None,
                            close_s=max(v.close_s for v in ds),
                            outcome=why,
                        )
                if iterations is None or num < iterations:
                    time.sleep(poll)
    finally:
        mt.shutdown()


def export_quotes(args, cfg):
    """Read-only export using the same exact broker-symbol resolution as the scanner."""
    from bot.zones.export import export

    from .config import HullConfig, selection

    try:
        import MetaTrader5 as mt
    except ImportError as exc:
        raise DemoSafetyError(
            "Run the read-only exporter on your Windows MT5 desktop"
        ) from exc
    profiles = selection(HullConfig.model_validate(cfg["hull"]), [args.symbol])
    path = cfg.get("mt5", {}).get("terminal_path", "")
    if not (mt.initialize(path) if path else mt.initialize()):
        raise DemoSafetyError(f"MT5 initialize failed: {mt.last_error()}")
    try:
        name = next(iter(resolve(mt, profiles, check_lots=False)))
        print(json.dumps(export(mt, name, args.start, args.end, args.out), indent=2))
    finally:
        mt.shutdown()
