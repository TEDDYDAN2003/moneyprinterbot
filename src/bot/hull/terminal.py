"""Single Python companion for the user's running Windows MT5 Desktop terminal."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from bot.execution.demo_runner import DemoSafetyError, _account_order_lock
from bot.zones.engine import Bar

from .config import (
    BATCH_ENTRY_POLICY,
    COUNTS,
    FRAMES,
    LADDER,
    LIVE_CONTINUATION,
    RETIRED_LIMITS,
    TARGET_POLICY,
)
from .desk import Desk, identity_check, resolve
from .entry_clock import LIVE_QUOTE_POLICY, QUOTE_POLICY, WAITING, poll_delay
from .ladder import cash_levels
from .live import LiveSampler
from .orders import Block, Grid, choose, select_labels, spread_goal
from .signal import Hull, LiveTick
from .structure import ENTRY_POLICY


def describe(r):
    return {
        **asdict(r),
        **r.entry_audit(),
        "direction": "BUY" if r.label == 1 else "SELL" if r.label == -1 else "NONE",
        **r.clock_fields(),
        "anchor_bar_utc": datetime.fromtimestamp(r.anchor_s, UTC).isoformat()
        if r.anchor_s is not None
        else None,
    }


class Stream:
    def __init__(self, tf, cfg, pip, *, live=False):
        self.live = live
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
        # Closed (authoritative) clock. Index zero is read separately and NEVER committed.
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
        updates = 0
        gap = False
        last = -1
        for r in rates:
            t = int(r["time"])
            if t <= last:
                raise DemoSafetyError(f"{name} M{self.tf}: unordered terminal bars")
            last = t
            if t + self.tf * 60 > now or (self.h.prev and t <= self.h.prev.t):
                continue
            gap |= self.h.prev is not None and t != self.h.prev.t + self.tf * 60
            updates += 1
            prior_side = self.h.structure.view.continuation_side
            decision = self.h.update(
                Bar(t, *[float(r[k]) for k in ("open", "high", "low", "close")])
            )
            if self.live and prior_side and decision.label == prior_side:
                # It belonged to the live path, even if polling missed it, it was
                # vetoed, or the scanner joined mid-bar. Still a real CLOSED exit label.
                decision = replace(decision, confirmation_only=True)
            if decision.label:
                self.last_label = decision
        # Reconnect/initial history may contain old labels. Never enter from warm-up.
        return (
            (None, "warmup_only")
            if reload or gap or updates > 1
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


def source_build():
    """Identify the actual imported checkout once; never require Git or network to scan."""
    module = Path(__file__).resolve()
    root = module.parents[3]
    head = None
    if (root / ".git").exists() and (root / "src/bot/hull/terminal.py") == module:
        try:
            r = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            candidate = r.stdout.strip()
            if (
                r.returncode == 0
                and len(candidate) in (40, 64)
                and all(c in "0123456789abcdef" for c in candidate)
            ):
                head = candidate
        except (OSError, subprocess.SubprocessError):
            pass
    return {"source_module": str(module), "checkout_head_at_start": head}


def dispatch(d, name, labels, *, data_ok, history_ok, rewarmed):
    """Handle fresh labels once; a pending tick-read never replays an opposite exit."""
    window = d.waiting.get(name)
    unavailable = not (data_ok and history_ok)
    if window is not None:
        stamp = window.reading.signal_msc
        # A native lane normally emits only once. Never replay its exit or reset
        # the window if the same closed reading is presented twice.
        labels = [
            r
            for r in labels
            if r.signal_msc != stamp or r.tf not in window.observed_lanes
        ]
        if (
            unavailable
            or rewarmed
            or any(r.side and r.signal_msc > stamp for r in labels)
        ):
            reason = (
                "data_or_deal_history_unavailable"
                if unavailable
                else "first_quote_wait_rewarmed"
                if rewarmed
                else "first_quote_wait_superseded"
            )
            d.cancel_wait(name, reason)
            window = None
    if not labels and window is None:
        return
    original, _ = choose(r for r in labels if not r.trigger)
    _, entry, why = select_labels(labels)
    raw_tf = original.tf if original else None
    if unavailable:
        why = "data_or_deal_history_unavailable"
    else:
        # This is intentionally the original, pre-structure opposite-label selector.
        if original is not None and d.h.indicator.exit_opposite:
            closed = d.close_opposite(name, original)
            if closed:
                d.emit(
                    "opposite_label_exit",
                    symbol=name,
                    tf=raw_tf,
                    signal=original.close_s,
                    closed_positions=closed,
                    next_side="BUY" if original.side == 1 else "SELL",
                )
        if window is not None:
            # MT5 may publish two native lanes for the SAME close on different polls.
            # Arbitrate those immutable readings before the first usable quote only.
            # No older label, later price or duplicate can grant a second reservation.
            if labels:
                same = [r for r in labels if r.signal_msc == window.reading.signal_msc]
                _, pick, why = select_labels([window.reading, *same])
                if pick is None:
                    d.cancel_wait(name, why)  # conflicting qualified directions
                    return
                window.observed_lanes.update(r.tf for r in same)
                # Do not convert a reserved closed-label wait into a live entry.
                if not pick.trigger:
                    window.reading = pick
            why = d.resume_first_quote(name)
            if why != WAITING:
                d.emit(
                    "label_decision",
                    symbol=name,
                    tf=window.reading.tf,
                    raw_selected_tf=window.raw_tf,
                    close_s=window.reading.close_s,
                    **window.reading.clock_fields(),
                    polls=window.polls,
                    outcome=why,
                )
            return
        if entry is not None:
            why = d.batch(
                name,
                entry,
                raw_tf=raw_tf,
                observed_lanes=(
                    r.tf for r in labels if r.signal_msc == entry.signal_msc
                ),
                live_peers=(r for r in labels if r.trigger and r.entry_side),
            )
    if why != WAITING:
        context = entry or max(labels, key=lambda r: r.signal_msc)
        d.emit(
            "label_decision",
            symbol=name,
            tf=entry.tf if entry else None,
            raw_selected_tf=raw_tf,
            close_s=context.close_s,
            **context.clock_fields(),
            outcome=why,
        )


def target_preview(mt, name, p, q, grid, h, now):
    """Read-only, indicative. BUY and SELL can have different cash conversions."""

    def cash(side, lots, entry, mark):
        typ = mt.ORDER_TYPE_BUY if side == 1 else mt.ORDER_TYPE_SELL
        return mt.order_calc_profit(typ, name, lots, entry, mark)

    result = {}
    for label, side in (("BUY", 1), ("SELL", -1)):
        try:
            if q is None or not 0 <= now - q.ms / 1000 <= h.limits.max_quote_age_s:
                raise Block("quote_unavailable_or_stale")
            if (
                p.max_spread is not None
                and q.ask - q.bid > p.max_spread + grid.tick * 1e-7
            ):
                raise Block("spread_cap")
            spread, goal = spread_goal(side, p.lots, q.bid, q.ask, p.target_usd, cash)
            result[label] = {
                "status": "indicative",
                "entry_spread_usd": spread,
                "initial_goal_usd": goal,
                "ladder_preview_per_ticket": [cash_levels(goal, n) for n in range(1, 5)]
                if h.exit_policy == LADDER
                else [],
                "batch_target_range_usd": [
                    min(COUNTS[p.rate]) * goal,
                    max(COUNTS[p.rate]) * goal,
                ],
            }
        except Block as exc:
            result[label] = {"status": str(exc)}
    return result


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
        live = h.entry_timing == LIVE_CONTINUATION
        specs = {}
        for name, (code, p) in syms.items():
            i = mt.symbol_info(name)
            g = Grid.from_info(i)
            q = LiveTick.read(mt.symbol_info_tick(name))
            specs[name] = {
                "key": code,
                "bid_chart_verified": getattr(i, "chart_mode", None)
                == getattr(mt, "SYMBOL_CHART_MODE_BID", 0),
                **p.model_dump(),
                **asdict(g),
                "contract_size": i.trade_contract_size,
                "profit_currency": i.currency_profit,
                "broker_trade_mode": i.trade_mode,
                "target_preview_by_side": target_preview(
                    mt, name, p, q, g, h, time.time()
                ),
                "preview_quote": asdict(q) if q else None,
                "legs_when_room": COUNTS[p.rate],
                "batch_lots_range": [
                    round(p.lots * min(COUNTS[p.rate]), 8),
                    round(p.lots * max(COUNTS[p.rate]), 8),
                ],
                "latest_spread": None if q is None else q.ask - q.bid,
            }
        summary = {
            "mode": "DEMO ARMED" if armed else "READ ONLY",
            "account": "USD hedging demo",
            "terminal_trade_allowed": bool(mt.terminal_info().trade_allowed),
            "account_expert_allowed": bool(a.trade_expert and a.trade_allowed),
            "timeframes": list(frames),
            "new_ticket_exit_policy": h.exit_policy,
            "new_ticket_target_policy": TARGET_POLICY,
            "entry_policy": ENTRY_POLICY,
            "signal_policy": h.indicator.signal_policy,
            "batch_entry_policy": BATCH_ENTRY_POLICY,
            "entry_timing": h.entry_timing,
            "entry_quote_policy": LIVE_QUOTE_POLICY if live else QUOTE_POLICY,
            **source_build(),
            "specs": specs,
            "limits": limits.model_dump(),
            "retired_limits": list(RETIRED_LIMITS),
            "indicator": h.indicator.model_dump(),
        }
        if preflight:
            from bot.zones.monitor import snapshot

            ps, os = snapshot(mt)
            summary["open_positions"] = len(ps)
            summary["pending_orders"] = len(os)
            summary["history_check"] = {}
            issues = []
            if not live:
                issues.append(
                    "Confirmed-close-only timing selected: no intrabar continuation entries"
                )
            legacy = [k for k in RETIRED_LIMITS if k in cfg["hull"].get("limits", {})]
            if legacy:
                issues.append(
                    "Retired Hull limits ignored in this config: " + ", ".join(legacy)
                )
            # An ignored local demo YAML is a full config, not an overlay on main.
            # Surface opt-ins so a stale copy is visible before arming.
            optional = [
                f"{key}={summary['limits'][key]:g}"
                for key in (
                    "max_positions",
                    "max_total_lots",
                    "max_open_risk_usd",
                    "min_free_margin_pct",
                )
                if summary["limits"][key]
            ]
            if optional:
                issues.append(
                    "Optional Hull entry limits enabled in this config; confirm intended: "
                    + ", ".join(optional)
                )
            for name, (_, p) in syms.items():
                checks = {}
                for side, preview in specs[name]["target_preview_by_side"].items():
                    if preview["status"] != "indicative":
                        issues.append(
                            f"{name} {side}: target preview unavailable: {preview['status']}"
                        )
                if live and not specs[name]["bid_chart_verified"]:
                    issues.append(
                        f"{name}: live continuation disabled; cannot verify native BID chart"
                    )
                for tf in frames:
                    lane = Stream(tf, h.indicator, p.pip_size, live=live)
                    _, status = lane.refresh(mt, name, time.time())
                    v = lane.last_label
                    checks[f"M{tf}"] = {
                        "status": status,
                        "closed_bars_loaded": lane.h.n,
                        "current_structure_phase": lane.h.structure.view.phase,
                        "last_label_for_comparison_only": describe(v) if v else None,
                    }
                    if status == "history_unavailable" or lane.h.n < lane.min_bars:
                        issues.append(f"{name} M{tf}: insufficient closed-bar history")
                summary["history_check"][name] = checks
            if h.indicator.source != "either" or h.indicator.hull_len != 16:
                issues.append(
                    "Non-default Hull signal settings: verify intended hull.indicator.source and hull_len; v2 defaults are either / 16."
                )
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
            locks.callback(d.cancel_all_waits)
            streams = {
                (name, tf): Stream(tf, h.indicator, p.pip_size, live=live)
                for name, (_, p) in syms.items()
                for tf in frames
            }
            samplers = {
                name: LiveSampler({tf: streams[name, tf] for tf in frames}, emit)
                for name in syms
                if live and specs[name]["bid_chart_verified"]
            }
            d.live_check = lambda name, r, q: samplers[name].still_crossed(
                mt, name, r, q
            )
            num = 0
            sync = 0.0
            history_ok = not armed
            emit("started", summary=summary)
            while iterations is None or num < iterations:
                num += 1
                now = time.time()
                d.account()  # identity, permissions and valid equity, even on quiet polls
                d.supervise()
                if armed and now - sync >= 15:
                    history_ok = sync_deals(d, now)
                    sync = now
                for name in syms:
                    d.service_if_due()
                    ds = []
                    data_ok = True
                    rewarmed = False
                    for tf in frames:
                        reading, why = streams[name, tf].refresh(mt, name, time.time())
                        rewarmed |= why == "warmup_only"
                        if why == "history_unavailable":
                            data_ok = False
                            emit("data_unavailable", symbol=name, tf=tf)
                        if reading and reading.source_conflict:
                            emit(
                                "source_cross_conflict",
                                symbol=name,
                                **describe(reading),
                            )
                        if reading and reading.label:
                            ds.append(reading)
                            emit(
                                "closed_label",
                                symbol=name,
                                detected_lag_ms=round(
                                    (time.time() - reading.close_s) * 1000, 3
                                ),
                                **describe(reading),
                            )
                    if name in samplers:
                        sampler = samplers[name]
                        if not data_ok or not history_ok:
                            sampler.disconnected = True
                        elif rewarmed:
                            sampler.reset(time.time())
                        else:
                            for r in sampler.sample(
                                mt, name, time.time, limits.max_quote_age_s
                            ):
                                emit(
                                    "live_hull_cross",
                                    symbol=name,
                                    detected_lag_ms=round(
                                        (time.time() - r.signal_s) * 1000, 3
                                    ),
                                    **describe(r),
                                )
                                if r.entry_side:
                                    ds.append(r)
                    dispatch(
                        d,
                        name,
                        ds,
                        data_ok=data_ok,
                        history_ok=history_ok,
                        rewarmed=rewarmed,
                    )
                if iterations is None or num < iterations:
                    time.sleep(
                        poll_delay(
                            time.time(),
                            frames,
                            poll,
                            limits.max_entry_age_s,
                            bool(
                                d.waiting
                                or (armed and d.ladders.book)
                                or any(s.watching() for s in samplers.values())
                            ),
                        )
                    )
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
