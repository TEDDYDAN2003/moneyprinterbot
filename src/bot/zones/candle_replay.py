"""Spread-aware, causal, tick-only price-action experiment, one pass for all arms."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

from bot.backtest.metrics import summarize_pnl

from .config import ZoneConfig
from .engine import Decision, ZoneEngine
from .feed import tick_minutes
from .hours import Hours
from .orders import EquityGuard, Plan, plan_entry, pnl, portfolio_block
from .price_action import (
    POLICIES,
    adverse,
    arm,
    cancel_at_close,
    evidence,
    execution_stop,
    protect,
    return_plan,
    rules_for,
    stop_bid,
)
from .replay import file_hash, iso_ms


@dataclass
class OpenTrade:
    d: Decision
    plan: Plan
    ms: int
    risk: float
    seq: int
    peak: float = 0.0


FIELDS = [
    "batch",
    "leg",
    "signal_utc",
    "entry_utc",
    "exit_utc",
    "side",
    "source_tf",
    "setup",
    "pattern",
    "previously",
    "source_pattern",
    "prior_at_zone",
    "evidence",
    "zone_id",
    "level",
    "width",
    "reaction_low",
    "reaction_high",
    "invalidation_bid",
    "stop_bid",
    "objective",
    "lots",
    "fill",
    "sl",
    "tp",
    "risk_usd",
    "cost_floor_usd",
    "peak_sampled_usd",
    "exit_fill",
    "exit",
    "net_usd",
]


def net(tr, s, bid, ask):
    side = tr.d.side
    return pnl(
        s, side, tr.plan.modeled_fill, (bid if side == 1 else ask) - side * s.slippage
    )


def exit_event(tr, m, begin, s, pa, samples, stop_fill, managed):
    """Work from begin onward, comparing INDEX not timestamp (ms may repeat)."""
    side = tr.d.side
    p = tr.plan
    q = m.bid if side == 1 else m.ask
    if begin >= len(q):
        return None
    arr = q[begin:]
    sl = side * (arr - p.sl) <= 0
    tp = side * (arr - p.tp) >= 0
    ix = np.flatnonzero(sl | tp)
    ei = len(q) if not len(ix) else begin + int(ix[0])
    if managed:
        for i in samples[(samples >= begin) & (samples < ei)]:
            marked = net(tr, s, float(m.bid[i]), float(m.ask[i]))
            tr.peak, close = protect(tr.peak, marked, s, pa)
            if close:
                return int(m.ms[i]), float(q[i]), "profit_giveback"
    if ei < len(q):
        stop = bool(sl[ei - begin])
        price = (float(q[ei]) if stop_fill == "first_quote" else p.sl) if stop else p.tp
        return int(m.ms[ei]), price, "stop" if stop else "target"
    return None


def stats(rows, balance):
    values = [r["net_usd"] for r in rows]
    m = summarize_pnl(values)
    m["trades"] = len(values)
    m["profit_factor"] = (
        m["profit_factor"] if math.isfinite(m["profit_factor"]) else None
    )
    if not values:
        m["win_rate"] = None
    m.update(
        wins=sum(x > 0 for x in values),
        expectancy_usd=sum(values) / len(values) if values else None,
        max_drawdown_pct_initial=100 * m["max_drawdown"] / balance,
        largest_loss=min([0.0, *values]),
        median_initial_risk=float(np.median([r["risk_usd"] for r in rows]))
        if rows
        else None,
        max_initial_risk=max((r["risk_usd"] for r in rows), default=None),
    )
    return m


def record(tr, s, pa, event, leg):
    ms, px, why = event
    p = tr.plan
    d = tr.d
    fill = px - d.side * s.slippage
    return dict(
        zip(
            FIELDS,
            [
                tr.seq,
                leg,
                iso_ms(d.t * 1000),
                iso_ms(tr.ms),
                iso_ms(ms),
                "BUY" if d.side == 1 else "SELL",
                f"M{d.tf}",
                d.reason,
                "|".join(d.candle),
                "|".join(d.previous_candle),
                "|".join(d.source_candle),
                d.prior_at_zone,
                evidence(d),
                d.zone_id,
                d.level,
                d.width,
                d.trigger_low,
                d.trigger_high,
                d.invalidation,
                stop_bid(d, s, pa),
                d.objective,
                s.lots,
                p.modeled_fill,
                p.sl,
                p.tp,
                round(tr.risk, 8),
                p.cost_floor_usd,
                tr.peak,
                fill,
                why,
                round(pnl(s, d.side, p.modeled_fill, fill), 8),
            ],
            strict=True,
        )
    )


class Paired:
    """Counterfactual on exactly the immediate-entry cohort, NOT a funded strategy."""

    def __init__(self, s, pa, stop_fill):
        self.s, self.pa, self.fill = s, pa, stop_fill
        self.active = []
        self.rows = []
        self.missing = 0

    def add(self, tr, bid, ask):
        d = tr.d
        s = self.s
        sl = execution_stop(stop_bid(d, s, self.pa), d.side, ask - bid, s.tick_size)
        mark = bid if d.side == 1 else ask
        if d.side * (mark - sl) < s.tick_size - 1e-10:
            self.missing += 1
            return
        p = replace(tr.plan, sl=sl)
        risk = -pnl(s, d.side, p.modeled_fill, sl - d.side * s.slippage)
        self.active.append(OpenTrade(d, p, tr.ms, risk, tr.seq))

    def step(self, m, flatten, samples):
        still = []
        for tr in self.active:
            # Frozen entries always occur at the first quote of an M1 bar.
            q = m.bid if tr.d.side == 1 else m.ask
            if (
                flatten
                and tr.d.side * (q[0] - tr.plan.sl) > 0
                and tr.d.side * (q[0] - tr.plan.tp) < 0
            ):
                ev = (int(m.ms[0]), float(q[0]), "scheduled_break")
            else:
                ev = exit_event(tr, m, 0, self.s, self.pa, samples, self.fill, False)
            if ev:
                self.rows.extend(
                    record(tr, self.s, self.pa, ev, i + 1) for i in range(self.s.legs)
                )
            else:
                still.append(tr)
        self.active = still

    def finish(self, m):
        for tr in self.active:
            q = m.bid if tr.d.side == 1 else m.ask
            self.rows.extend(
                record(
                    tr,
                    self.s,
                    self.pa,
                    (int(m.ms[-1]), float(q[-1]), "end_of_sample"),
                    i + 1,
                )
                for i in range(self.s.legs)
            )
        self.active = []


class Account:
    def __init__(self, cfg, z, key, policy, stop_fill, paired=None):
        self.s = z.symbols[key]
        self.pa = z.price_action.model_copy(update={"policy": policy})
        self.lim = z.limits
        self.rules = rules_for(z.rules, policy)
        self.fill = stop_fill
        self.paired = paired
        self.initial = self.eq = float(cfg["backtest"]["initial_balance"])
        self.guard = EquityGuard()
        self.pos = self.w = None
        self.rows = []
        self.count = Counter()
        self.quotes = Counter()
        self.seq = 0

    def close(self, event):
        for i in range(self.s.legs):
            row = record(self.pos, self.s, self.pa, event, i + 1)
            self.rows.append(row)
            self.eq += row["net_usd"]
        self.pos = None

    def enter(self, d, p, m, i):
        self.seq += 1
        s = self.s
        self.pos = OpenTrade(
            d,
            p,
            int(m.ms[i]),
            -pnl(s, d.side, p.modeled_fill, p.sl - d.side * s.slippage),
            self.seq,
        )
        if self.paired:
            self.paired.add(self.pos, m.bid[i], m.ask[i])
        self.count["entries"] += 1
        self.w = None

    def drop(self, why):
        if self.w:
            self.count["watch:" + why] += 1
            self.w = None

    def step(self, m, d, samples, block, flatten, day):
        pa = self.pa
        s = self.s
        pol = pa.policy
        t = m.bar.t
        ms = int(m.ms[0])
        bid, ask = float(m.bid[0]), float(m.ask[0])
        start_i = 0
        fresh = d is not None and d.t == t
        if self.pos:
            p = self.pos.plan
            q = bid if p.side == 1 else ask
            if p.side * (q - p.sl) <= 0:
                self.close((ms, q if self.fill == "first_quote" else p.sl, "stop"))
            elif p.side * (q - p.tp) >= 0:
                self.close((ms, p.tp, "target"))
            elif flatten:
                self.close((ms, q, "scheduled_break"))
            elif (
                pol.endswith("_managed")
                and fresh
                and d.t * 1000 > self.pos.ms
                and (reason := adverse(d, p.side, pa))
            ):
                self.close((ms, q, reason))
        halted = self.guard.mark(
            day,
            self.eq + (s.legs * net(self.pos, s, bid, ask) if self.pos else 0.0),
            self.lim,
        )
        if d and d.side:
            self.count["zone_candidates"] += 1
        if not self.pos:
            gate = block or halted or portfolio_block(s, self.lim, 0, 0.0, 0.0)
            if pol == "baseline":
                if d and d.side:
                    why = gate or (
                        "stale_signal"
                        if not fresh or ms / 1000 - t > self.lim.entry_age_seconds
                        else ""
                    )
                    if not why:
                        p, why = plan_entry(d, s, self.rules, bid, ask)
                        if p:
                            self.enter(d, p, m, 0)
                    if why != "ready":
                        self.count["not_entered:" + why] += 1
            else:
                if self.w:
                    why = gate or (
                        "data_gap" if not fresh else cancel_at_close(self.w, d, ms)
                    )
                    if why:
                        self.drop(why)
                if (
                    fresh
                    and d.side
                    and not self.w
                    and not gate
                    and ms / 1000 - t <= self.lim.entry_age_seconds
                ):
                    self.w, why = arm(d, s, pa)
                    self.count["signal:" + why] += 1
                if self.w:
                    w = self.w
                    side = w.d.side
                    invalid = side * (m.bid - w.invalid_bid) <= 0
                    if w.d.objective is not None:
                        invalid |= side * (m.bid - w.d.objective) >= 0
                    invalid |= m.ms >= w.until_ms
                    ix = np.flatnonzero(invalid)
                    cut = int(ix[0]) if len(ix) else len(m.ms)
                    for i in samples[samples < cut]:
                        p, why = return_plan(
                            w,
                            s,
                            self.rules,
                            float(m.bid[i]),
                            float(m.ask[i]),
                            int(m.ms[i]),
                        )
                        self.quotes[why] += 1
                        if p:
                            start_i = int(i)
                            self.enter(w.d, p, m, i)
                            break
                    if self.w and cut < len(m.ms):
                        self.drop(
                            "expired"
                            if m.ms[cut] >= w.until_ms
                            else w.crossed(m.bid[cut])
                        )
        if self.pos:
            ev = exit_event(
                self.pos,
                m,
                start_i,
                s,
                pa,
                samples,
                self.fill,
                pol.endswith("_managed"),
            )
            if ev:
                self.close(ev)
        self.guard.mark(
            day,
            self.eq
            + (
                s.legs * net(self.pos, s, float(m.bid[-1]), float(m.ask[-1]))
                if self.pos
                else 0
            ),
            self.lim,
        )

    def finish(self, m):
        if self.pos:
            q = m.bid if self.pos.d.side == 1 else m.ask
            self.close((int(m.ms[-1]), float(q[-1]), "end_of_sample"))
        self.drop("end_of_sample")


def write_rows(path, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def benchmark(
    minutes,
    cfg,
    key,
    windows,
    policies=POLICIES,
    stop_fill="barrier",
    out=None,
    paired=True,
):
    """Ordered nonoverlapping [start,end) windows, prior signal history only. No future labels."""
    if stop_fill not in ("barrier", "first_quote"):
        raise ValueError("Unknown stop fill")
    if (
        not windows
        or any(a >= b for _, a, b in windows)
        or any(windows[i][2] > windows[i + 1][1] for i in range(len(windows) - 1))
    ):
        raise ValueError("Invalid windows")
    if not policies or any(k not in POLICIES for k in policies):
        raise ValueError("Unknown policy")
    z = ZoneConfig.model_validate(cfg["zones"])
    s = z.symbols[key]
    eng = ZoneEngine(z.rules, s)
    hours = Hours(cfg)
    cadence = max(1, round(z.price_action.poll_seconds * 1000))
    dst = Path(out) if out is not None else None
    result = {}
    i = 0
    ctx = None
    active = None
    last = None

    def begin():
        diagnostic = (
            Paired(s, z.price_action, stop_fill)
            if "baseline" in policies and paired
            else None
        )
        return (
            {
                k: Account(
                    cfg, z, key, k, stop_fill, diagnostic if k == "baseline" else None
                )
                for k in policies
            },
            diagnostic,
            Counter(),
            Counter(),
        )

    def finish(name, state, last):
        accounts, diag, ct, tags = state
        folder = dst / name if dst else None
        if folder:
            folder.mkdir(parents=True, exist_ok=True)
        res = {
            "coverage": dict(ct),
            "last_utc": iso_ms(int(last.ms[-1])),
            "pattern_tags_on_zone_candidates": dict(tags),
            "arms": {},
        }
        for k, a in accounts.items():
            a.finish(last)
            res["arms"][k] = {
                "metrics": stats(a.rows, a.initial),
                "events": dict(a.count),
                "watch_quote_checks": dict(a.quotes),
                "exits": dict(Counter(r["exit"] for r in a.rows)),
                "guard": asdict(a.guard),
            }
            if folder:
                write_rows(folder / (k + ".csv"), a.rows)
        if diag:
            diag.finish(last)
            risks = [r["risk_usd"] for r in diag.rows]
            base = accounts["baseline"]
            measured = stats(diag.rows, base.initial)
            measured.pop("max_drawdown")
            measured.pop("max_drawdown_pct_initial")
            res["unbounded_same_entries_diagnostic"] = {
                "metrics": measured,
                "over_budget": sum(r > s.sl_usd + 1e-8 for r in risks),
                "unavailable": diag.missing,
                "note": "Frozen signals only. Possible overlapping exposure. Not a deployable portfolio or a $5-risk alternative.",
            }
            if folder:
                write_rows(folder / "unbounded_same_entries.csv", diag.rows)
        if folder:
            (folder / "summary.json").write_text(
                json.dumps(res, indent=2, allow_nan=False) + "\n"
            )
        result[name] = res

    for m in minutes:
        if not m.exact:
            raise ValueError(
                "Intraminute retests need ordered bid/ask ticks, not synthetic OHLC paths"
            )
        t = m.bar.t
        while i < len(windows) and t >= windows[i][2]:
            if active is not None:
                finish(windows[i][0], active, last)
            active = None
            i += 1
        if i == len(windows):
            break
        if t >= windows[i][1]:
            if active is None:
                active = begin()
            acc, diag, ct, tags = active
            if not ct:
                ct["first_ms"] = int(m.ms[0])
            ct["bars"] += 1
            ct["ticks"] += len(m.ms)
            ct["open_spread_ok"] += int(m.ask[0] - m.bid[0] <= s.max_spread + 1e-10)
            samples = np.r_[0, 1 + np.flatnonzero(np.diff(m.ms // cadence))]
            block, flatten = hours.status(t)
            day = iso_ms(t * 1000)[:10]
            if ctx and ctx.side:
                tags.update(ctx.candle)
            for a in acc.values():
                a.step(m, ctx, samples, block, flatten, day)
            if diag:
                diag.step(m, flatten, samples)
            last = m
        ctx = eng.update(m.bar)
    if active is not None:
        finish(windows[i][0], active, last)
    if len(result) != len(windows):
        raise ValueError("No quotes for one or more requested windows")
    return result


def run_price_test(args, cfg):
    paths = args.ticks
    z = ZoneConfig.model_validate(cfg["zones"])
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    lo = int(pd.to_datetime(args.start, utc=True).timestamp()) if args.start else 0
    hi = int(pd.to_datetime(args.end, utc=True).timestamp()) if args.end else 2**40
    policies = (
        POLICIES if args.policy == "all" else (args.policy or z.price_action.policy,)
    )
    result = benchmark(
        tick_minutes(paths),
        cfg,
        args.symbol,
        [("window", lo, hi)],
        policies,
        args.stop_fill,
        out,
    )
    (out / "manifest.json").write_text(
        json.dumps(
            {
                "inputs": {Path(p).name: file_hash(p) for p in paths},
                "symbol": args.symbol,
                "zones": z.model_dump(),
                "stop_fill": args.stop_fill,
                "utc_window_inclusive_exclusive_s": [lo, hi],
                "policies": policies,
                "config_sha256": hashlib.sha256(
                    json.dumps(cfg, sort_keys=True).encode()
                ).hexdigest(),
            },
            indent=2,
        )
        + "\n"
    )
    for k, a in result["window"]["arms"].items():
        print(k, json.dumps(a["metrics"], allow_nan=False))
