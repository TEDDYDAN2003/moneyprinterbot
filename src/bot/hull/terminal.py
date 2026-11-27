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

from bot.broker import DemoSafetyError, _account_order_lock
from bot.config.entry_sessions import sessions_for_runner

from . import retest
from .config import (
    BATCH_ENTRY_POLICY,
    COUNTS,
    EXECUTION_INFO,
    INTRA_ENTRY,
    INTRA_QUOTE,
    INTRA_STOP,
    INTRABAR,
    LADDER,
    M1_MODE,
    NATIVE_TF,
    PAIR_MODE,
    RETEST,
    RETIRED_LIMITS,
    STOP_SPREAD_POLICY,
    TARGET_POLICY,
    TF_NAME,
)
from .curves import LiveTick
from .desk import Desk, identity_check, resolve
from .entry_clock import CLOCK_SKEW_TOLERANCE_S, QUOTE_POLICY, WAITING, poll_delay
from .feed import Stream, StructureTape, sample, verify_before_leg
from .intrabar_book import IntrabarBook
from .intrabar_guard import valid_origin
from .ladder import cash_levels
from .minute import MinuteBook
from .orders import Block, Grid, spread_goal
from .pairs import PairBook, asof


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
        # Audit only: a manual, SL or TP closing deal can carry a different magic.
        # Do not treat that magic (or a similar comment) as position ownership.
        lane = d.owners.deal_lane(x, d.syms, d.h.magic)
        linked_exit = lane is not None and x.entry in (
            mt.DEAL_ENTRY_OUT,
            mt.DEAL_ENTRY_OUT_BY,
        )
        if (d.owned(x) or linked_exit) and str(x.ticket) not in known:
            d.emit(
                "broker_deal",
                tf=lane,
                attribution="reconciled_position_exit"
                if linked_exit
                else "symbol_and_magic",
                **{
                    k: getattr(x, k, None)
                    for k in (
                        "symbol",
                        "ticket",
                        "position_id",
                        "order",
                        "magic",
                        "reason",
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


def dispatch(d, name, labels, *, data_ok, history_ok, rewarmed, shifts=()):
    """All protective actions before new entries. No cross-timeframe voting or occupancy."""
    now = time.time()
    for r in sorted(
        (*shifts, *(r for r in labels if getattr(r, "confirmed_mss", False))),
        key=lambda r: r.close_s,
    ):
        d.close_mss(name, r)
    events, exited, live_seen = {}, set(), {}
    for r in sorted(labels, key=lambda v: v.signal_msc):
        if (
            not r.label
            or TF_NAME.get(r.tf) not in d.h.eligible_timeframes
            or not 0 <= now - r.signal_s <= d.lim.max_entry_age_s
            or (r.trigger and now >= r.close_s)
        ):
            continue
        w = d.waiting.get((name, r.tf))
        if (
            not r.trigger
            and w
            and (r.signal_msc, r.label) == (w.reading.signal_msc, w.reading.label)
        ):
            continue
        matched = d.close_opposite(name, r)
        if matched:
            exited.add((r.tf, r.signal_msc, -r.label))
            d.emit(
                "opposite_label_exit",
                symbol=name,
                tf=r.tf,
                signal=r.signal_s,
                matched_positions=matched,
                raw_side=r.label,
                **r.clock_fields(),
            )
        if r.trigger is not None:
            # Protect immediately, but never compete as an entry with the just-closed
            # candle. A newer opposing observation invalidates that lane's candidate.
            live_seen[r.tf, r.label] = r.signal_msc
        else:
            events.setdefault(r.tf, []).append(r)
    frames = set(events) | {tf for sym, tf in d.waiting if sym == name}
    for tf in sorted(frames):
        w = d.waiting.get((name, tf))
        rows = events.get(tf, [])
        healthy = data_ok.get(tf, False) if isinstance(data_ok, dict) else data_ok
        warming = rewarmed.get(tf, False) if isinstance(rewarmed, dict) else rewarmed
        if not (healthy and history_ok) or warming:
            why = (
                "data_or_deal_history_unavailable"
                if not (healthy and history_ok)
                else "warmup_only"
            )
            if w:
                d.cancel_wait(
                    name,
                    tf,
                    "first_quote_wait_rewarmed" if why == "warmup_only" else why,
                )
            if rows:
                d.emit(
                    "label_decision",
                    symbol=name,
                    tf=tf,
                    outcome=why,
                    signal_session=d.session_gate(name).report(rows[-1].signal_s),
                    **rows[-1].clock_fields(),
                    **rows[-1].entry_audit(),
                )
            continue
        latest = max((r.signal_msc for r in rows), default=-1)
        if w and latest > w.reading.signal_msc:
            d.cancel_wait(name, tf, "first_quote_wait_superseded")
            w = None
        if w and latest < w.reading.signal_msc:
            rows = []
        peers = [r for r in rows if r.signal_msc == latest] + ([w.reading] if w else [])
        if not peers:
            continue
        valid = [r for r in peers if r.entry_side]
        r = (valid or peers)[-1]
        if live_seen.get((tf, -r.label), -1) >= r.signal_msc:
            why = "opposite_after_confirmed_cross"
        elif len({v.label for v in peers}) > 1:
            why = "conflicting_signals_same_lane_tick"
        elif (tf, r.signal_msc, r.entry_side) in exited:
            why = "same_side_held_at_opposite_exit"
        elif w:
            why = d.resume_first_quote(name, tf)
        elif valid:
            why = d.batch(name, r, raw_tf=tf)
        else:
            why = r.entry_reason
        if w and why in (
            "opposite_after_confirmed_cross",
            "conflicting_signals_same_lane_tick",
            "same_side_held_at_opposite_exit",
        ):
            d.cancel_wait(name, tf, why)
        elif why != WAITING:
            d.emit(
                "label_decision",
                symbol=name,
                tf=tf,
                raw_selected_tf=tf,
                close_s=r.close_s,
                **r.clock_fields(),
                **r.entry_audit(),
                signal_session=d.session_gate(name).report(r.signal_s),
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
            if q is None or not -CLOCK_SKEW_TOLERANCE_S <= now - q.ms / 1000 <= h.limits.max_quote_age_s:
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


def make_lanes(h, frames, p, tick_size):
    tf = NATIVE_TF.get(h.indicator.structure_timeframe)
    ext = (
        StructureTape(tf, h.indicator.swing_len, max(frames), h.indicator.pullback_bars)
        if tf and any(f != tf for f in frames)
        else None
    )
    return ext, {
        f: Stream(
            f, h.indicator, p.pip_size, ext if f != tf else None, tick_size=tick_size
        )
        for f in frames
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
    minute_mode = h.execution_mode == M1_MODE
    if minute_mode and (
        set(frames) - {5, 15, 30, 45}
        or h.indicator.structure_timeframe not in (None, "Chart")
    ):
        raise ValueError("M1 execution requires native M5/M15/M30/M45 parent structure")
    paired = h.execution_mode == PAIR_MODE
    if paired and (
        set(frames) - h.pairs.keys() or h.indicator.structure_timeframe is not None
    ):
        raise ValueError(
            "Paired execution needs native M5/M10/M15 parent structure and structure_timeframe: null"
        )
    live_pair = h.effective_timing == INTRABAR
    if live_pair and (not paired or h.entry_route == RETEST):
        raise ValueError("intrabar_cross requires paired market execution")
    limits_mode = h.entry_route == RETEST
    if limits_mode and not paired:
        raise ValueError("direct_retest_limit requires paired_structure")
    pairs = {p: h.pairs[p] for p in frames} if paired else {}
    native_frames = (
        tuple(sorted(set(frames) | set(pairs.values()))) if paired else frames
    )
    exposure_books = 1 if minute_mode or paired else len(frames)
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
        sessions = sessions_for_runner()
        session_issues = []
        specs = {}
        for name, (code, p) in syms.items():
            i = mt.symbol_info(name)
            g = Grid.from_info(i)
            q = LiveTick.read(mt.symbol_info_tick(name))
            gate = sessions.bind(name, code)
            if gate.issue:
                session_issues.append(f"{name} ({code}): {gate.issue}; no new entries")
            specs[name] = {
                "key": code,
                **(
                    {"retest_capability": retest.capability(mt, i) or "supported"}
                    if limits_mode
                    else {}
                ),
                "bid_chart_verified": getattr(i, "chart_mode", None)
                == getattr(mt, "SYMBOL_CHART_MODE_BID", 0),
                **p.model_dump(),
                "entry_session": gate.report(time.time()),
                **asdict(g),
                "contract_size": i.trade_contract_size,
                "profit_currency": i.currency_profit,
                "broker_trade_mode": i.trade_mode,
                "target_preview_by_side": target_preview(
                    mt, name, p, q, g, h, time.time()
                ),
                "preview_quote": asdict(q) if q else None,
                "legs_when_room": COUNTS[p.rate],
                "max_tickets_across_selected_lanes": exposure_books
                * max(COUNTS[p.rate]),
                "max_lots_across_selected_lanes": round(
                    exposure_books * max(COUNTS[p.rate]) * p.lots, 8
                ),
                "batch_lots_range": [
                    round(p.lots * min(COUNTS[p.rate]), 8),
                    round(p.lots * max(COUNTS[p.rate]), 8),
                ],
                "latest_spread": None if q is None else q.ask - q.bid,
            }
        root = Path("reports/hull_runtime") / key
        audit = root / ("demo.jsonl" if armed else "shadow.jsonl")
        summary = {
            "account_key": key,
            "journal_magic": h.magic,
            "audit_log": str(audit.resolve()),
            "mode": "DEMO ARMED" if armed else "READ ONLY",
            "account": "USD hedging demo",
            "terminal_trade_allowed": bool(mt.terminal_info().trade_allowed),
            "account_expert_allowed": bool(a.trade_expert and a.trade_allowed),
            "timeframes": [TF_NAME[f] for f in frames],
            "structure_timeframe": h.indicator.structure_timeframe or "Chart",
            "new_ticket_exit_policy": h.exit_policy,
            "new_ticket_breakeven_policy": h.new_ticket_breakeven_policy,
            "new_ticket_target_policy": retest.GOAL if limits_mode else TARGET_POLICY,
            "entry_route": h.entry_route,
            **(
                {
                    "limit_policy": retest.POLICY,
                    "retest_fraction": h.retest_fraction,
                    "retest_atr_cap": h.retest_atr_cap,
                    "fill_expiration": "successor_trigger_close_or_earlier_session_end",
                }
                if limits_mode
                else {}
            ),
            "execution_mode": h.execution_mode,
            "trigger_timeframe": "per_parent"
            if paired
            else "M1"
            if minute_mode
            else "same_as_parent",
            **(
                {
                    "parent_to_trigger": {
                        TF_NAME[p]: TF_NAME[c] for p, c in pairs.items()
                    },
                    "structure_alignment": "last_closed_parent_and_trigger_at_cross_tick"
                    if live_pair
                    else "own_parent_and_trigger_at_trigger_close",
                }
                if paired
                else {}
            ),
            "entry_policy": INTRA_ENTRY
            if live_pair
            else EXECUTION_INFO[h.execution_mode][0],
            "entry_sessions": sessions.identity(),
            "signal_policy": h.indicator.signal_policy,
            "batch_entry_policy": "single_symbol_highest_qualifying_parent"
            if minute_mode or paired
            else BATCH_ENTRY_POLICY,
            "entry_timing": h.effective_timing,
            "entry_quote_policy": INTRA_QUOTE if live_pair else QUOTE_POLICY,
            "new_ticket_stop_policy": INTRA_STOP
            if live_pair
            else EXECUTION_INFO[h.execution_mode][1],
            "stop_spread_policy": STOP_SPREAD_POLICY,
            "live_cross_role": "paired_market_entry_and_protection"
            if live_pair
            else "protective_exit_only",
            **(
                {
                    "origin_cross_exit": "own_source_through_original_close_including_equality",
                    "same_candle_reentry": "new_edge_after_verified_flat_only",
                }
                if live_pair
                else {}
            ),
            "opposite_exit": "own_trigger_only_plus_parent_protection"
            if paired
            else "symbol_wide_opposing_m1_for_verified_m1_tickets_plus_parent_protection"
            if minute_mode
            else "lane_raw_cross_before_entry_checks",
            "mss_exit": "flatten_own_lane_on_confirmed_shift",
            "ownership": "verified_position_id_symbol_parent_and_trigger"
            if paired
            else "verified_position_id_and_broker_symbol_and_signal_timeframe",
            **(
                {"m45_clock": "3 consecutive M15 bars anchored to the broker D1 open"}
                if not paired
                else {}
            ),
            "choch_entry": "never_standalone",
            "post_mss_entry": "first_aligned_cross_allowed"
            if paired and not h.skip_first_cross_after_mss
            else "skip_first_except_failed_counter_mss_return",
            "failed_mss_return": "confirmed_MSS_back_to_established_bias_without_counter_BOS; fresh_cross_after_close_only",
            "price_zone_filter": False,
            **source_build(),
            "specs": specs,
            "limits": limits.model_dump(),
            "retired_limits": list(RETIRED_LIMITS),
            "indicator": h.indicator.model_dump(),
        }
        unsupported = [
            n
            for n, x in specs.items()
            if limits_mode and x["retest_capability"] != "supported"
        ]
        if preflight:
            from bot.broker import snapshot

            ps, os = snapshot(mt)
            summary["open_positions"] = len(ps)
            summary["pending_orders"] = len(os)
            summary["history_check"] = {}
            issues = list(session_issues) + [
                n + ": " + specs[n]["retest_capability"] for n in unsupported
            ]
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
                ext, lanes = make_lanes(h, native_frames, p, specs[name]["tick"])
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
                        "bar_source": "3 x M15, broker D1 anchor"
                        if tf == 45
                        else "2 x M4, broker D1 anchor"
                        if tf == 8
                        else "native MT5",
                        "closed_bars_loaded": lane.h.n,
                        "current_structure_phase": active.phase,
                        **(
                            {
                                "last_native_cross_for_comparison_only": {
                                    "chart_tf": tf,
                                    "close_s": v.close_s,
                                    "raw_side": v.label,
                                    "closed_bias": v.structure.bias,
                                    "wick": v.anchor,
                                }
                                if v
                                else None
                            }
                            if paired
                            else {
                                "last_label_for_comparison_only": describe(v)
                                if v
                                else None
                            }
                        ),
                    }
                    if paired:
                        checks[TF_NAME[tf]].update(
                            parent=tf in pairs,
                            trigger_for=[
                                TF_NAME[p] for p, c in pairs.items() if c == tf
                            ],
                            last_structure_close_s=active.closed_s,
                            native_history_ready=lane.h.n >= lane.min_bars
                            and asof(active, tf, now),
                        )
                        if live_pair and tf in pairs.values():
                            checks[TF_NAME[tf]]["last_confirmed_trigger_swing"] = (
                                asdict(active.range_swing)
                                | {"kind": "HL" if active.bias == 1 else "LH"}
                                if active.range_swing
                                else None
                            )
                        if not asof(active, tf, now):
                            issues.append(
                                f"{name} {TF_NAME[tf]}: no current closed structure bar"
                            )
                    if status == "history_unavailable" or lane.h.n < lane.min_bars:
                        issues.append(
                            f"{name} {TF_NAME[tf]}: insufficient closed-bar history"
                        )
                if minute_mode:
                    minute = Stream(
                        1,
                        h.indicator,
                        p.pip_size,
                        tick_size=specs[name]["tick"],
                        price_only=True,
                    )
                    _, status = minute.refresh(mt, name, time.time())
                    checks["M1 trigger"] = {
                        "status": status,
                        "closed_bars_loaded": minute.h.n,
                        "role": "timing_and_opposite_exit_only",
                    }
                    if not minute.h.prev or status == "history_unavailable":
                        issues.append(
                            f"{name}: native M1 history unavailable; no M1 entries"
                        )
                summary["history_check"][name] = checks
            if ps or os:
                issues.append(
                    "Preflight found existing exposure; review before starting or changing profiles"
                )
            if minute_mode:
                summary["m1_history_ready"] = all(
                    specs[name]["bid_chart_verified"]
                    and all(
                        row["status"] != "history_unavailable"
                        and row["closed_bars_loaded"] > 0
                        for row in checks.values()
                    )
                    for name, checks in summary["history_check"].items()
                )
            if paired:
                summary["pair_history_ready"] = all(
                    specs[name]["bid_chart_verified"]
                    and all(
                        row["status"] != "history_unavailable"
                        and row["native_history_ready"]
                        for row in checks.values()
                    )
                    for name, checks in summary["history_check"].items()
                )
            summary["review_before_demo"] = issues
        print(json.dumps(summary, indent=2, allow_nan=False), flush=True)
        if preflight:
            if unsupported:
                raise DemoSafetyError(
                    "Retest pending capability unverified: " + ", ".join(unsupported)
                )
            if session_issues:
                raise DemoSafetyError("; ".join(session_issues))
            if minute_mode and not summary["m1_history_ready"]:
                raise DemoSafetyError(
                    "M1 or approved parent bid history unavailable; do not arm"
                )
            if paired and not summary["pair_history_ready"]:
                raise DemoSafetyError(
                    "Paired native bid history unavailable or stale; do not arm"
                )
            return summary
        root.mkdir(parents=True, exist_ok=True)

        def emit(event, **fields):
            line = json.dumps(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "event": event,
                    "scanner_magic": h.magic,
                    **fields,
                },
                allow_nan=False,
            )
            with audit.open("a", encoding="utf8") as f:
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
            d = Desk(
                mt, cfg, h, syms, identity, state, path, emit, armed, sessions=sessions
            )
            locks.callback(d.cancel_all_waits)
            bundles = {
                name: make_lanes(h, native_frames, p, specs[name]["tick"])
                for name, (_, p) in syms.items()
            }

            # Context settings are part of the identity: history in another lane
            # cannot consume this lane's skip. The new stop mode gets its own
            # confirmed-only reconstruction. Desk still honors pre-upgrade order
            # reservations; no ticket state is migrated or replayed.
            policy_key = hashlib.sha256(
                h.indicator.model_dump_json(exclude={"exit_on_mss"}).encode()
            ).hexdigest()[:16]
            d.indicator_key = policy_key
            trigger_books = {}
            if paired:
                records = state.setdefault("pair_parent_first_cross", {}).setdefault(
                    policy_key, {}
                )
                book_type = IntrabarBook if live_pair else PairBook
                trigger_books = {
                    name: book_type(
                        frames,
                        lanes,
                        records.setdefault(name, {}),
                        pairs=h.pairs,
                        skip_first=h.skip_first_cross_after_mss,
                    )
                    for name, (_, lanes) in bundles.items()
                }
            elif minute_mode:
                records = state.setdefault("m1_parent_first_cross", {}).setdefault(
                    policy_key, {}
                )
                trigger_books = {
                    name: MinuteBook(
                        h,
                        syms[name][1],
                        specs[name]["tick"],
                        lanes,
                        records.setdefault(name, {}),
                        d.persist if armed else None,
                    )
                    for name, (_, lanes) in bundles.items()
                }
            else:
                skip_book = state.setdefault("first_cross_after_mss", {}).setdefault(
                    policy_key, {}
                )
                for name, (_, lanes) in bundles.items():
                    for tf, lane in lanes.items():
                        record = skip_book.setdefault(name, {}).setdefault(str(tf), {})
                        lane.h.gate.bind(record, d.persist if armed else None)

            if d.retest:
                d.retest.recover(frames)
                if (
                    armed
                    and unsupported
                    and not (
                        d.retest.active
                        or d.exits
                        or d.ladders.book
                        or state.get("target_by_ticket")
                    )
                ):
                    raise DemoSafetyError(
                        "Required broker limit/bracket/specified-expiry capability missing: "
                        + ", ".join(unsupported)
                    )
            if armed and (
                paired or any(rec.get("trigger_tf") for rec in d.owners.book.values())
            ):
                inventory = mt.positions_get()
                if inventory is None:
                    raise DemoSafetyError(
                        "Cannot verify manager coverage before starting"
                    )
                for pos in inventory:
                    origin = d.owners.origin(pos)
                    saved = (
                        d.owners.book.get(str(getattr(pos, "identifier", pos.ticket)))
                        or {}
                    )
                    if (
                        origin is None
                        and saved.get("trigger_tf")
                        and saved.get("symbol") == pos.symbol
                        and saved.get("magic") == pos.magic
                    ):
                        raise DemoSafetyError(
                            "Unverified Hull ticket identity; do not switch managers while open"
                        )
                    child = (origin or {}).get("trigger_tf")
                    if (
                        origin
                        and "intrabar_origin" in origin
                        and not valid_origin(
                            origin["intrabar_origin"],
                            child,
                            1 if pos.type == mt.POSITION_TYPE_BUY else -1,
                            origin["opened_msc"],
                        )
                    ):
                        raise DemoSafetyError("Open intrabar-origin ticket has unverified cross identity; reconcile before changing managers while open")
                    covered = (
                        pos.magic == h.magic
                        and pos.symbol in syms
                        and origin is not None
                        and origin["tf"] in frames
                    )
                    if child == 1 and not (
                        minute_mode
                        and covered
                        and origin.get("m1_policy_key") == policy_key
                    ):
                        raise DemoSafetyError(
                            "Open M1-origin ticket needs its original symbol, parent, indicator settings and M1 manager; do not change modes while open"
                        )
                    if child not in (None, 1) and not (
                        paired
                        and covered
                        and pairs.get(origin["tf"]) == child
                        and origin.get("pair_policy_key") == policy_key
                        and (not origin.get("intrabar_origin") or live_pair)
                    ):
                        raise DemoSafetyError(
                            "Open paired-origin ticket needs its original symbol, parent/trigger and indicator settings; do not change managers while open"
                        )
                    if (
                        paired
                        and child is None
                        and (origin or pos.magic == h.magic)
                        and not (d.retest and d.retest.claims(pos))
                    ):
                        raise DemoSafetyError(
                            "Unverified or legacy Hull exposure: do not switch to paired execution while open"
                        )

            def verify(name, r, quote):
                # Take care of an MSS even between legs of a slow batch on this
                # instrument. A sibling shift protects the sibling, not this batch.
                invalidated = False
                for f, stream in bundles[name][1].items():
                    if f not in frames:
                        continue  # a child-only MSS is not a parent exit
                    for shift in stream.boundary_shifts(mt, name, time.time()):
                        d.confirmed_shift(name, f, shift)
                        if f == r.tf and shift.closed_s * 1000 >= r.signal_msc:
                            invalidated = True
                if r.trigger_tf is not None:
                    ok = trigger_books[name].verify(d, name, r, quote, time.time)
                    return ok and not invalidated
                if invalidated:
                    return False

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

            if trigger_books:

                def other_protection(active_symbol):
                    for symbol in {sym for sym, _ in d.open_lanes} - {active_symbol}:
                        if (
                            symbol in trigger_books
                            and specs[symbol]["bid_chart_verified"]
                        ):
                            trigger_books[symbol].watch(d, symbol, time.time)

                d.watch_other_symbols = other_protection
            d.verify_signal = verify
            if d.retest:
                locks.callback(d.retest.shutdown)
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
                    if trigger_books:
                        trigger_books[name].poll(
                            d,
                            name,
                            time.time,
                            history_ok=history_ok,
                            bid_chart=specs[name]["bid_chart_verified"],
                        )
                        continue
                    ds, data_ok, rewarmed = [], {}, {}
                    ext, lanes = bundles[name]
                    ext_status = (
                        ext.refresh(mt, name, time.time()) if ext else "updated"
                    )
                    valid_lanes = []
                    for tf, lane in lanes.items():
                        if lane.ext and ext_status == "history_unavailable":
                            data_ok[tf] = False
                            lane.baseline_due = True
                            continue
                        reading, why = lane.refresh(mt, name, time.time())
                        data_ok[tf] = (
                            why != "history_unavailable"
                            and specs[name]["bid_chart_verified"]
                        )
                        if data_ok[tf]:
                            for shift in lane.shifts:
                                d.close_mss(
                                    name, shift
                                )  # independent of labels, history sync or entry age
                        warming = why == "warmup_only" or (
                            lane.ext and ext_status == "warmup_only"
                        )
                        if warming:
                            lane.baseline_due = rewarmed[tf] = True
                        if (
                            why == "history_unavailable"
                            or not specs[name]["bid_chart_verified"]
                        ):
                            data_ok[tf] = False
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
                                established_bias=v.established_bias,
                                failed_counter_mss_s=v.failed_counter_mss_s,
                                is_failed_mss_return=abs(v.event_code) == 2
                                and v.failed_return,
                                bias=v.bias,
                                broken_level=v.broken.price,
                                protected_level=v.guard.price if v.guard else None,
                            )
                        if reading and lane.note(reading, baseline=bool(warming)):
                            ds.append(reading)
                            emit("closed_label", symbol=name, **describe(reading))
                    for lane in valid_lanes:
                        # One bad/incomplete M45 projection cannot veto a usable M15/M30 signal.
                        live, _ = sample(
                            mt, name, [lane], time.time, limits.max_quote_age_s
                        )
                        for reading in live:
                            ds.append(reading)
                            emit("live_hull_cross", symbol=name, **describe(reading))
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
                            (1, *frames) if minute_mode else native_frames,
                            min(0.1, poll) if trigger_books else poll,
                            limits.max_entry_age_s,
                            True,  # always watch for a live cross, including exit-only signals
                        )
                    )
    finally:
        mt.shutdown()


def export_quotes(args, cfg):
    """Read-only export using the same exact broker-symbol resolution as the scanner."""
    from bot.hull.quote_export import export

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
