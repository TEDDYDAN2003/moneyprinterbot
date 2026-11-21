"""Single Python companion for the user's running Windows MT5 Desktop terminal."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from bot.execution.demo_runner import DemoSafetyError, _account_order_lock

from .config import (
    BATCH_ENTRY_POLICY,
    COUNTS,
    ENTRY_POLICY,
    NATIVE_TF,
    TF_NAME,
    LADDER,
    RETIRED_LIMITS,
    TARGET_POLICY,
)
from .desk import Desk, identity_check, resolve
from .entry_clock import LIVE_QUOTE_POLICY, WAITING, poll_delay
from .ladder import cash_levels
from .orders import Block, Grid, spread_goal
from .curves import LiveTick
from .feed import Stream, StructureTape, sample, verify_before_leg


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
    """Raw exits first (even if another lane or deal history is unavailable).
    Only trend-aligned entries vote. Never reopen on an older signal or hedge
    simultaneously qualified opposite directions. A stale close is not a fresh cross.
    """
    now = time.time()
    labels = [
        r
        for r in labels
        if r.label
        and TF_NAME.get(r.tf) in d.h.eligible_timeframes
        and 0 <= now - r.signal_s <= d.lim.max_entry_age_s
        and (not r.trigger or now < r.close_s)
    ]
    window = d.waiting.get(name)
    if window:
        labels = [
            r
            for r in labels
            if r.signal_msc != window.reading.signal_msc
            or r.tf not in window.observed_lanes
        ]
    exited_sides = set()
    for r in sorted(labels, key=lambda r: r.signal_msc):
        matched = d.close_opposite(name, r)
        if matched:
            exited_sides.add((r.signal_msc, -r.label))
            d.emit(
                "opposite_label_exit",
                symbol=name,
                tf=r.tf,
                signal=r.signal_s,
                matched_positions=matched,
                raw_side=r.label,
                **r.clock_fields(),
            )
    if not (data_ok and history_ok) or rewarmed:
        reason = (
            "data_or_deal_history_unavailable"
            if not (data_ok and history_ok)
            else "warmup_only"
        )
        if window:
            d.cancel_wait(
                name,
                "first_quote_wait_rewarmed"
                if rewarmed and reason == "warmup_only"
                else reason,
            )
        if labels:
            d.emit(
                "label_decision",
                symbol=name,
                tf=labels[-1].tf,
                outcome=reason,
                **labels[-1].clock_fields(),
            )
        return
    latest = max((r.signal_msc for r in labels), default=-1)
    if window and latest > window.reading.signal_msc:
        d.cancel_wait(name, "first_quote_wait_superseded")
        window = None
    peers = [r for r in labels if r.signal_msc == latest]
    if window:
        peers = [window.reading] + [
            r for r in peers if r.signal_msc == window.reading.signal_msc
        ]
    if not peers:
        return
    valid = [r for r in peers if r.entry_side]
    context = max(valid or peers, key=lambda r: r.tf)
    conflict = len({r.entry_side for r in valid}) > 1
    if conflict:
        why = "conflicting_trend_signals_same_tick"
        if window:
            d.cancel_wait(name, why)
            return
    elif (context.signal_msc, context.entry_side) in exited_sides:
        # This direction was held at the trigger. Don't reopen it on the very
        # signal that flattened it, even if the broker deal log is lagging.
        why = "same_side_held_at_opposite_exit"
        if window:
            d.cancel_wait(name, why)
            return
    elif window:
        window.observed_lanes.update(r.tf for r in peers)
        window.reading = context
        why = d.resume_first_quote(name)
    elif valid:
        why = d.batch(
            name, context, raw_tf=context.tf, observed_lanes=[r.tf for r in peers]
        )
    else:
        why = context.entry_reason
    if why != WAITING:
        d.emit(
            "label_decision",
            symbol=name,
            tf=context.tf,
            raw_selected_tf=context.tf,
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


def make_lanes(h, frames, p):
    tf = NATIVE_TF.get(h.indicator.structure_timeframe)
    ext = (
        StructureTape(tf, h.indicator.swing_len, max(frames), h.indicator.pullback_bars)
        if tf and any(f != tf for f in frames)
        else None
    )
    return ext, {
        f: Stream(f, h.indicator, p.pip_size, ext if f != tf else None) for f in frames
    }


def run(
    cfg,
    h,
    profiles,
    frames=None,
    armed=False,
    preflight=False,
    poll=1.0,
    iterations=None,
):
    # No timeframe flag, old local YAML, or direct invocation can enable an unapproved lane.
    frames = h.lanes(frames)
    if not 0.25 <= poll <= 10 or (iterations is not None and iterations < 1):
        raise ValueError("Invalid poll/iterations")
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
            "timeframes": [TF_NAME[f] for f in frames],
            "structure_timeframe": h.indicator.structure_timeframe or "Chart",
            "new_ticket_exit_policy": h.exit_policy,
            "new_ticket_target_policy": TARGET_POLICY,
            "entry_policy": ENTRY_POLICY,
            "signal_policy": h.indicator.signal_policy,
            "batch_entry_policy": BATCH_ENTRY_POLICY,
            "entry_timing": h.entry_timing,
            "entry_quote_policy": LIVE_QUOTE_POLICY,
            "opposite_exit": "raw_cross_before_entry_checks",
            "choch_entry": "never_standalone",
            "price_zone_filter": False,
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
                ext, lanes = make_lanes(h, frames, p)
                now = time.time()
                if not specs[name]["bid_chart_verified"]:
                    issues.append(
                        f"{name}: bid chart unverified; no signals from this symbol"
                    )
                if ext and ext.refresh(mt, name, now) == "history_unavailable":
                    issues.append(
                        f"{name}: native structure timeframe history unavailable"
                    )
                for tf, lane in lanes.items():
                    _, status = lane.refresh(mt, name, now)
                    v = lane.last_label
                    active = (
                        ext.at(tf, lane.h.prev.t, now)
                        if lane.ext and lane.h.prev
                        else lane.h.structure.view
                    )
                    checks[TF_NAME[tf]] = {
                        "status": status,
                        "closed_bars_loaded": lane.h.n,
                        "current_structure_phase": active.phase,
                        "last_label_for_comparison_only": describe(v) if v else None,
                    }
                    if status == "history_unavailable" or lane.h.n < lane.min_bars:
                        issues.append(
                            f"{name} {TF_NAME[tf]}: insufficient closed-bar history"
                        )
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
            locks.callback(d.cancel_all_waits)
            bundles = {name: make_lanes(h, frames, p) for name, (_, p) in syms.items()}

            def verify(name, r, quote):
                def interrupt(opposite):
                    emit(
                        "live_hull_cross",
                        symbol=name,
                        during_batch=True,
                        **describe(opposite),
                    )
                    matched = d.close_opposite(name, opposite)
                    emit(
                        "batch_interrupted_by_opposite",
                        symbol=name,
                        tf=opposite.tf,
                        matched_positions=matched,
                        **opposite.clock_fields(),
                    )

                return verify_before_leg(
                    mt,
                    name,
                    bundles[name][1],
                    r,
                    quote,
                    time.time,
                    limits.max_quote_age_s,
                    interrupt,
                )

            d.verify_signal = verify
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
                    ds, data_ok, rewarmed = [], True, False
                    ext, lanes = bundles[name]
                    ext_status = (
                        ext.refresh(mt, name, time.time()) if ext else "updated"
                    )
                    valid_lanes = []
                    for tf, lane in lanes.items():
                        if lane.ext and ext_status == "history_unavailable":
                            data_ok = False
                            lane.baseline_due = True
                            continue
                        reading, why = lane.refresh(mt, name, time.time())
                        warming = why == "warmup_only" or (
                            lane.ext and ext_status == "warmup_only"
                        )
                        if warming:
                            lane.baseline_due = rewarmed = True
                        if (
                            why == "history_unavailable"
                            or not specs[name]["bid_chart_verified"]
                        ):
                            data_ok = False
                            lane.baseline_due = True
                            continue
                        valid_lanes.append(lane)
                        if (
                            reading
                            and reading.structure.event_code
                            and reading.structure.closed_s == reading.close_s
                        ):
                            v = reading.structure
                            emit(
                                "structure_break",
                                symbol=name,
                                tf=tf,
                                close_s=v.closed_s,
                                event_code=v.event_code,
                                bias=v.bias,
                                broken_level=v.broken.price,
                                protected_level=v.guard.price if v.guard else None,
                            )
                        if reading and lane.note(reading, baseline=bool(warming)):
                            ds.append(reading)
                            emit("closed_label", symbol=name, **describe(reading))
                    if valid_lanes:
                        live, coherent = sample(
                            mt, name, valid_lanes, time.time, limits.max_quote_age_s
                        )
                        for reading in live:
                            ds.append(reading)
                            emit("live_hull_cross", symbol=name, **describe(reading))
                        # A confirmed close may still wait for its first fresh post-close quote.
                        # A partial set of live projections cannot authorize a live batch.
                        if live and not coherent:
                            data_ok = False
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
                            True,  # always watch for a live cross, including exit-only signals
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
