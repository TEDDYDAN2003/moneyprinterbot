"""Next-quote execution with an identical signal/price plan to the demo scanner."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import subprocess
from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from bot.backtest.metrics import summarize_pnl
from bot.decision.market_aware import evaluate_market

from .config import ZoneConfig
from .engine import Decision, ZoneEngine
from .feed import bar_minutes, tick_minutes
from .hours import Hours
from .orders import EquityGuard, Plan, plan_entry, pnl, portfolio_block, reversal


@dataclass
class Holding:
    d: Decision
    plan: Plan
    ms: int
    batch: int


def hit(m, p, stop_fill="barrier"):
    """Return first touched exit (or conservative stop if OHLC order unknowable)."""
    q = m.bid if p.side == 1 else m.ask

    def at(i, tag):
        return (
            int(m.ms[i]),
            float(
                (q[i] if stop_fill == "first_quote" else p.sl)
                if tag == "stop"
                else p.tp
            ),
            tag,
        )

    stop, target = p.side * (q - p.sl) <= 0, p.side * (q - p.tp) >= 0
    if stop[0] or target[0]:
        return at(0, "stop" if stop[0] else "target")
    if m.exact:
        indices = np.flatnonzero(stop | target)
        if len(indices):
            i = indices[0]
            return at(i, "stop" if stop[i] else "target")
    elif stop.any():
        return int(m.ms[-1]), p.sl, "stop_ambiguous" if target.any() else "stop"
    elif target.any():
        return int(m.ms[-1]), p.tp, "target"
    return None


def legacy_signal(history, bid, ask, cfg, t):
    """Frozen legacy evaluate_market rules; one decision per minute, not a tick-worker reproduction."""
    p = cfg["pilot"]
    levels = cfg["levels"]
    b = pd.DataFrame(
        history, columns=("time", "open", "high", "low", "close", "volume", "spread")
    )
    b.index = pd.to_datetime(b.pop("time"), unit="s", utc=True)
    for tf in (
        "M15",
        "M5",
        "M1",
    ):  # deterministic tie-break in place of three workers racing
        try:
            d = evaluate_market(
                b,
                tf,
                bid,
                ask,
                min_score=p["min_confluence_score"],
                target_r_multiple=p["target_r_multiple"][tf],
                minimum_target_price_units=0.01,
                minimum_net_reward_risk=0.01,
                max_friction_to_stop_fraction=1000.0,
                round_trip_cost_ex_spread_price_units=p[
                    "round_trip_cost_ex_spread_price_units"
                ],
                sessions=cfg["sessions"],
                require_protected_stop=False,
                confluence_weights=p["confluence_weights"],
                level_proximity_atr=levels["proximity_atr"],
                minimum_opposing_level_room_atr=levels[
                    "minimum_opposing_level_room_atr"
                ],
                minimum_pullback_rejection=levels["minimum_pullback_rejection"],
                pullback_proximity_by_timeframe=levels[
                    "pullback_proximity_by_timeframe"
                ],
            )
        except ValueError as exc:
            if not str(exc).startswith("Need at least"):
                raise
            continue
        if d.side:
            return Decision(
                t,
                "legacy_" + str(d.details["entry_location_setup"]),
                1 if d.side == "BUY" else -1,
                int(tf[1:]),
            )
    return Decision(t, "no_legacy_candidate")


TRADE_FIELDS = [
    "batch",
    "leg",
    "signal_close_utc",
    "entry_utc",
    "exit_utc",
    "side",
    "timeframe",
    "setup",
    "zone_id",
    "pool",
    "level",
    "width",
    "momentum",
    "objective",
    "lots",
    "entry_quote",
    "modeled_entry",
    "stop",
    "target",
    "exit_fill",
    "cost_floor_usd",
    "clears_structure",
    "structural_stop",
    "risk_at_structure_usd",
    "max_lots_for_structure",
    "exit",
    "net_usd",
]


def iso_ms(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="milliseconds")


def replay(
    minutes,
    cfg,
    key,
    start=None,
    end=None,
    strategy="zones",
    decision_log=None,
    stop_fill="barrier",
):
    if stop_fill not in ("barrier", "first_quote"):
        raise ValueError("Unknown stop fill convention")
    z = ZoneConfig.model_validate(cfg["zones"])
    s, r, limits = z.symbols[key], z.rules, z.limits
    if strategy == "legacy" and (
        key != "XAUUSD" or r.exit_mode != "fixed_cash" or r.require_structural_stop
    ):
        raise ValueError("Matched legacy control requires XAUUSD fixed cash barriers")
    engine, guard, hours = ZoneEngine(r, s), EquityGuard(), Hours(cfg)
    history = deque(maxlen=int(cfg["pilot"]["history_m1_bars"]))
    pos = last = prev = None
    trades, rejects, reasons, candidates = [], Counter(), Counter(), Counter()
    batches = ticks = bars = warmup = tight = 0
    spreads = []
    equity = float(cfg["backtest"]["initial_balance"])
    initial = equity
    if not math.isfinite(initial) or initial <= 0:
        raise ValueError("initial_balance must be positive")
    first = None

    def close(event):
        nonlocal pos, equity
        ms, quote, why = event
        fill = quote - pos.plan.side * s.slippage
        net = pnl(s, pos.plan.side, pos.plan.modeled_fill, fill)
        p, d = pos.plan, pos.d
        for leg in range(s.legs):
            trades.append(
                dict(
                    zip(
                        TRADE_FIELDS,
                        [
                            pos.batch,
                            leg + 1,
                            iso_ms(d.t * 1000),
                            iso_ms(pos.ms),
                            iso_ms(ms),
                            "BUY" if d.side > 0 else "SELL",
                            f"M{d.tf}",
                            d.reason,
                            d.zone_id,
                            d.pool,
                            d.level,
                            d.width,
                            d.momentum,
                            d.objective,
                            s.lots,
                            p.requested,
                            p.modeled_fill,
                            p.sl,
                            p.tp,
                            fill,
                            p.cost_floor_usd,
                            p.clears_structure,
                            p.structural_stop,
                            p.structural_risk_usd,
                            p.max_structure_lots,
                            why,
                            round(net, 8),
                        ],
                        strict=True,
                    )
                )
            )
            equity += net
        pos = None

    for m in minutes:
        b = m.bar
        if end is not None and b.t >= end:
            break
        if start is None or b.t >= start:
            first = b.t if first is None else first
            bars += 1
            ticks += len(m.ms) if m.exact else 0
            ms = int(m.ms[0])
            bid, ask = float(m.bid[0]), float(m.ask[0])
            spreads.append(ask - bid)
            tight += int(ask - bid <= s.max_spread + 1e-10)
            day = iso_ms(ms)[:10]
            block, flatten = hours.status(ms / 1000)
            modeled_mark = bid if pos and pos.plan.side == 1 else ask
            if pos:
                early = hit_open(m, pos.plan, stop_fill)
                if early:
                    close(early)
                elif flatten:
                    close((ms, modeled_mark, "scheduled_break"))
                elif prev and reversal(prev, pos.plan.side, r):
                    close((ms, modeled_mark, "adverse_structure"))
            mtm = equity + (
                s.legs
                * pnl(
                    s,
                    pos.plan.side,
                    pos.plan.modeled_fill,
                    modeled_mark - pos.plan.side * s.slippage,
                )
                if pos
                else 0
            )
            risk_block = guard.mark(day, mtm, limits)
            d = prev
            if (
                strategy == "legacy"
                and prev is not None
                and not pos
                and not block
                and not risk_block
            ):
                # Precheck universal cash/spread feasibility before expensive legacy scoring.
                feasible = any(
                    plan_entry(Decision(b.t, "probe", side), s, r, bid, ask)[0]
                    is not None
                    for side in (-1, 1)
                )
                d = (
                    legacy_signal(history, bid, ask, cfg, b.t)
                    if feasible and prev.t == b.t
                    else Decision(prev.t, "legacy_ineligible_quote")
                )
            elif strategy == "legacy":
                d = None
            outcome = "observe"
            if d:
                reasons[d.reason] += 1
            if d and d.side:
                candidates[d.reason] += 1
                outcome = (
                    "symbol_busy"
                    if pos
                    else "stale_signal"
                    if d.t != b.t or ms / 1000 - d.t > limits.entry_age_seconds
                    else block or risk_block or portfolio_block(s, limits, 0, 0.0, 0.0)
                )
                if not outcome:
                    plan, outcome = plan_entry(d, s, r, bid, ask)
                    if plan:
                        batches += 1
                        pos = Holding(d, plan, ms, batches)
                if outcome != "ready":
                    rejects[outcome] += 1
            if decision_log and d:
                print(
                    json.dumps(
                        dict(**asdict(d), bid=bid, ask=ask, outcome=outcome),
                        allow_nan=False,
                    ),
                    file=decision_log,
                )
            if pos:
                event = hit(m, pos.plan, stop_fill)
                if event:
                    close(event)
            mark = float(m.bid[-1] if pos and pos.plan.side == 1 else m.ask[-1])
            guard.mark(
                day,
                equity
                + (
                    s.legs
                    * pnl(
                        s,
                        pos.plan.side,
                        pos.plan.modeled_fill,
                        mark - pos.plan.side * s.slippage,
                    )
                    if pos
                    else 0.0
                ),
                limits,
            )
            last = m
        else:
            warmup += 1
        prev = engine.update(b)
        if strategy == "legacy":
            history.append(
                (
                    b.t,
                    b.o,
                    b.h,
                    b.l,
                    b.c,
                    m.volume,
                    (m.ask[-1] - m.bid[-1]) / s.point,
                )
            )
    if not bars:
        raise ValueError("No observations in replay interval")
    if pos:
        close(
            (
                int(last.ms[-1]),
                float(last.bid[-1] if pos.plan.side == 1 else last.ask[-1]),
                "end_of_sample",
            )
        )
    metrics = summarize_pnl(t["net_usd"] for t in trades)
    metrics["profit_factor"] = (
        metrics["profit_factor"] if math.isfinite(metrics["profit_factor"]) else None
    )
    if not trades:
        metrics["win_rate"] = None
    metrics["trades"] = len(trades)
    result = {
        "strategy": strategy,
        "instrument": key,
        "start_utc": iso_ms(first * 1000),
        "end_bar_utc": iso_ms(last.bar.t * 1000),
        "bars": bars,
        "historical_warmup_bars": warmup,
        "ticks": ticks,
        "batches": batches,
        "starting_equity_usd": initial,
        "median_open_spread": float(np.median(spreads)),
        "spread_eligible_minutes": tight,
        "decisions": dict(reasons),
        "candidates": dict(candidates),
        "blocked": dict(rejects),
        "exits": dict(Counter(t["exit"] for t in trades)),
        "drawdown_basis": "realized USD, after costs, from initial equity peak; not intratick equity drawdown",
        "metrics": metrics,
        "final_guard": asdict(guard),
        "stop_fill": stop_fill,
        "execution": "next observed minute opening quote; configured commission and slippage per side; executable-side barrier tests; modeled fills",
    }
    return result, trades


def hit_open(m, p, stop_fill="barrier"):
    q = float(m.bid[0] if p.side == 1 else m.ask[0])
    if p.side * (q - p.sl) <= 0:
        return int(m.ms[0]), q if stop_fill == "first_quote" else p.sl, "stop"
    if p.side * (q - p.tp) >= 0:
        return int(m.ms[0]), p.tp, "target"
    return None


def run_replay(args, cfg):
    if args.exit_mode is not None:
        cfg["zones"].setdefault("rules", {})["exit_mode"] = args.exit_mode
    if args.require_structural_stop:
        cfg["zones"].setdefault("rules", {})["require_structural_stop"] = True
    z = ZoneConfig.model_validate(cfg["zones"])
    if args.symbol not in z.symbols:
        raise ValueError("Symbol not configured")
    if args.spread_override is not None and not (
        math.isfinite(args.spread_override) and args.spread_override >= 0
    ):
        raise ValueError("spread override must be finite and nonnegative")
    data = (
        tick_minutes(args.ticks, args.spread_override)
        if args.ticks
        else bar_minutes(
            args.csv, z.symbols[args.symbol], args.source_tz, args.spread_override
        )
    )

    def stamp(v):
        return (
            None
            if v is None
            else int(pd.to_datetime(v, utc=True, errors="raise").timestamp())
        )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = (
        (out / "decisions.jsonl").open("w", encoding="utf-8")
        if args.decisions
        else None
    )
    try:
        result, trades = replay(
            data,
            cfg,
            args.symbol,
            stamp(args.start),
            stamp(args.end),
            args.strategy,
            log,
            args.stop_fill,
        )
    finally:
        if log:
            log.close()
    result.update(
        quote_mode="ticks" if args.ticks else "bar_proxy_stop_first",
        spread_override=args.spread_override,
        source_timezone=args.source_tz,
        experiment_config={
            "zones": z.model_dump(),
            "sessions": cfg.get("sessions", {}),
            "market_close_timezone": cfg["run"].get("market_close_timezone", "UTC"),
            "daily_flatten_time": cfg["pilot"]["daily_flatten_time"],
            "daily_reopen_time": cfg["pilot"]["daily_reopen_time"],
        },
        config_sha256=hashlib.sha256(
            json.dumps(cfg, sort_keys=True).encode()
        ).hexdigest(),
        input_files={Path(v).name: file_hash(v) for v in (args.ticks or [args.csv])},
    )
    try:
        result["git_head"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        result["git_head"] = None
    (out / "summary.json").write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    with (out / "trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        w.writeheader()
        w.writerows(trades)
    print(json.dumps(result["metrics"], indent=2))
    print(out)


def file_hash(path):
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()
