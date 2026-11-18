"""Single-symbol M1/M2/M5 quote-order check. Not a broker execution simulator."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from bot.backtest.metrics import summarize_pnl
from bot.zones.engine import Bar
from bot.zones.feed import tick_minutes
from bot.zones.hours import Hours
from bot.zones.orders import EquityGuard
from bot.zones.replay import file_hash, iso_ms

from .orders import Block, Exposure, Grid, choose, count_for, make_plan
from .signal import Hull


class Clock:
    """Aggregate the available bid candles. Emit only after the frame is closed."""

    def __init__(self, tf, cfg, pip):
        self.tf = tf
        self.h = Hull(tf, cfg, pip)
        self.parts = []
        self.start = None
        self.partial = False

    def before(self, m):
        start = m.t // (self.tf * 60) * (self.tf * 60)
        r = None
        if self.start != start:
            if self.parts and not self.partial:
                v = self.parts
                r = self.h.update(
                    Bar(
                        self.start,
                        v[0].o,
                        max(b.h for b in v),
                        min(b.l for b in v),
                        v[-1].c,
                    )
                )
            self.partial = self.start is None and m.t != start
            self.start = start
            self.parts = []
        self.parts.append(m)
        return r


class Account:
    def __init__(self, p, lim, grid, cash, hours, initial):
        self.p, self.lim, self.grid, self.cash, self.hours = p, lim, grid, cash, hours
        self.eq = initial
        self.guard = EquityGuard()
        self.pos = None
        self.rows = []
        self.count = Counter()
        self.labels = Counter()
        self.sends = []

    def guard_halt(self, day, equity):
        if not math.isfinite(equity) or equity <= 0:
            return "invalid_equity"
        if self.guard.day != day:
            self.guard.day = day
            self.guard.start = equity
            self.guard.day_halt = False
        self.guard.peak = max(self.guard.peak, equity)
        self.guard.dd_halt |= (
            self.guard.peak - equity >= self.lim.drawdown_usd - 1e-8
        )
        return "drawdown_halt" if self.guard.dd_halt else ""

    def close(self, why, ms, px):
        d, p, n, stamp = self.pos
        per = self.cash(p.side, p.lots, p.entry, px)
        net = per * n
        self.eq += net
        self.rows.append(
            {
                "signal": iso_ms(d.close_s * 1000),
                "tf": f"M{d.tf}",
                "side": "BUY" if p.side == 1 else "SELL",
                "anchor_time": iso_ms(d.anchor_s * 1000),
                "anchor": d.anchor,
                "raw_sl": d.raw_sl,
                "entry_time": iso_ms(stamp),
                "exit_time": iso_ms(ms),
                "legs": n,
                "lots_per_leg": p.lots,
                "entry": p.entry,
                "sl": p.sl,
                "tp": p.tp,
                "risk_usd_batch": round(p.risk_usd * n, 8),
                "exit_reason": why,
                "exit_price": px,
                "net_usd_per_leg": round(per, 8),
                "net_usd_batch": round(net, 8),
            }
        )
        self.pos = None

    def barriers(self, m, lo, hi):
        if not self.pos or lo >= hi:
            return
        _, p, _, _ = self.pos
        q = (m.bid if p.side == 1 else m.ask)[lo:hi]
        sl = p.side * (q - p.sl) <= 0
        tp = p.side * (q - p.tp) >= 0
        ix = np.flatnonzero(sl | tp)
        if len(ix):
            j = int(ix[0])
            self.close(
                "stop" if sl[j] else "target",
                int(m.ms[lo + j]),
                float(q[j]) if sl[j] else p.tp,
            )

    def equity(self, bid, ask):
        if not self.pos:
            return self.eq
        _, p, n, _ = self.pos
        return (
            self.eq
            + self.cash(p.side, p.lots, p.entry, bid if p.side == 1 else ask) * n
        )

    def step(self, m, ds):
        ms = int(m.ms[0])
        now = ms / 1000
        day = iso_ms(ms)[:10]
        bid = float(m.bid[0])
        ask = float(m.ask[0])
        self.barriers(m, 0, 1)
        halt = self.guard_halt(day, self.equity(bid, ask))
        labels = [v for v in ds if v.label]
        self.labels.update(
            f"M{d.tf}:{'BUY' if d.label == 1 else 'SELL'}" for d in labels
        )
        if labels:
            d, why = choose(labels)
            if d:
                if self.pos and self.pos[1].side != d.side:
                    old = self.pos[1]
                    exit_px = bid if old.side == 1 else ask
                    self.close("opposite_hull_label", ms, exit_px)
                    why = "opposite_label_exit"
                why = (
                    "symbol_busy"
                    if self.pos
                    else "stale_label_or_quote"
                    if not d.close_s <= now <= d.close_s + self.lim.max_entry_age_s
                    else halt
                    or (
                        self.hours.status(now)[0]
                        if self.p.session == "configured"
                        else ""
                    )
                )
                if not why:
                    try:
                        p = make_plan(d, self.p, self.grid, bid, ask, self.cash)
                        self.sends = [s for s in self.sends if s > now - 60]
                        n = count_for(
                            self.p.rate,
                            self.lim,
                            Exposure(),
                            p,
                            lambda n: (
                                "order_rate_limit"
                                if len(self.sends) + n > self.lim.max_orders_minute
                                else ""
                            ),
                        )
                        self.pos = (d, p, n, ms)
                        self.sends += [now] * n
                        why = "opened"
                    except Block as exc:
                        why = str(exc)
            self.count[why] += 1
        self.barriers(m, 0 if self.pos and self.pos[3] == ms else 1, len(m.ms))
        self.guard_halt(day, self.equity(float(m.bid[-1]), float(m.ask[-1])))

    def finish(self, last):
        if self.pos:
            p = self.pos[1]
            self.close(
                "sample_end",
                int(last.ms[-1]),
                float(last.bid[-1] if p.side == 1 else last.ask[-1]),
            )
        vals = [r["net_usd_batch"] for r in self.rows]
        metrics = summarize_pnl(vals)
        metrics["profit_factor"] = (
            metrics["profit_factor"]
            if math.isfinite(metrics["profit_factor"])
            else None
        )
        if not vals:
            metrics["win_rate"] = None
        metrics.pop("trades")
        metrics.update(
            batches=len(vals),
            wins=sum(v > 0 for v in vals),
            positions=sum(r["legs"] for r in self.rows),
            exits=dict(Counter(r["exit_reason"] for r in self.rows)),
            label_counts=dict(self.labels),
            decision_counts=dict(self.count),
        )
        return metrics


FIELDS = [
    "signal",
    "tf",
    "side",
    "anchor_time",
    "anchor",
    "raw_sl",
    "entry_time",
    "exit_time",
    "legs",
    "lots_per_leg",
    "entry",
    "sl",
    "tp",
    "risk_usd_batch",
    "exit_reason",
    "exit_price",
    "net_usd_per_leg",
    "net_usd_batch",
]


def study(minutes, cfg, h, p, frames, lo, hi, unit, contract, conversion, rates, out):
    if not (
        math.isfinite(unit)
        and math.isfinite(contract)
        and min(unit, contract) > 0
        and lo < hi
    ):
        raise ValueError("Invalid quote contract or interval")
    grid = Grid(
        unit,
        unit,
        max(0, -Decimal(str(unit)).normalize().as_tuple().exponent),
        0,
        1e-8,
        1e5,
        1e-8,
    )

    def cash(side, lots, start, end):
        return (
            side
            * lots
            * contract
            * (end - start)
            / (end if conversion == "base_usd" else 1.0)
        )

    clocks = [Clock(tf, h.indicator, p.pip_size) for tf in frames]
    acc = {
        r: Account(
            p.model_copy(update={"rate": r}),
            h.limits,
            grid,
            cash,
            Hours(cfg),
            cfg["backtest"]["initial_balance"],
        )
        for r in rates
    }
    last = None
    ticks = bars = 0
    labels = []
    for m in minutes:
        if not m.exact:
            raise ValueError("Use ordered bid/ask ticks")
        if m.bar.t >= hi:
            break
        ds = [r for c in clocks if (r := c.before(m.bar)) is not None]
        if m.bar.t < lo:
            continue
        for d in ds:
            if d.label:
                labels.append({"closed_utc": iso_ms(d.close_s * 1000), **asdict(d)})
        for a in acc.values():
            a.step(m, ds)
        last = m
        bars += 1
        ticks += len(m.ms)
    if last is None:
        raise ValueError("No quotes in window")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    result = {
        "bars": bars,
        "ticks": ticks,
        "rates": {},
        "timeframes": frames,
        "per_ticket_target": p.target_usd,
        "profile": p.model_dump(),
        "indicator": h.indicator.model_dump(),
        "limits": h.limits.model_dump(),
        "contract_size": contract,
        "tick_size": unit,
        "conversion": conversion,
        "start_utc": iso_ms(lo * 1000),
        "end_exclusive_utc": iso_ms(hi * 1000),
        "note": "USD, spread only. Actual crossing quote for SL; TP at limit. Account equity marked at minute boundaries. No broker margin/volume checks, latency or guarantee of atomic batch fills.",
    }
    from .signal import Reading

    with (out / "labels.csv").open("w", newline="") as f:
        w = csv.DictWriter(
            f, ["closed_utc", *Reading.__dataclass_fields__], lineterminator="\n"
        )
        w.writeheader()
        w.writerows(labels)
    for r, a in acc.items():
        result["rates"][r] = a.finish(last)
        with (out / (r + ".csv")).open("w", newline="") as f:
            w = csv.DictWriter(f, FIELDS, lineterminator="\n")
            w.writeheader()
            w.writerows(a.rows)
    (out / "summary.json").write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n"
    )
    return result


def run_replay(args, cfg, h, profiles, frames):
    if len(profiles) != 1:
        raise ValueError("Supply exactly one symbol per replay")
    p = next(iter(profiles.values()))
    ts = lambda x: int(pd.to_datetime(x, utc=True).timestamp())
    out = Path(args.out)
    result = study(
        tick_minutes(args.ticks),
        cfg,
        h,
        p,
        frames,
        ts(args.start),
        ts(args.end),
        args.tick_size,
        args.contract_size,
        args.conversion,
        (p.rate,),
        out,
    )
    (out / "inputs.json").write_text(
        json.dumps({str(path): file_hash(path) for path in args.ticks}, indent=2) + "\n"
    )
    print(json.dumps(result["rates"], indent=2))
