"""Single-symbol M1/M2/M5 quote-order check. Not a broker execution simulator."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pandas as pd

from bot.backtest.metrics import summarize_pnl
from bot.zones.engine import Bar
from bot.zones.feed import tick_minutes
from bot.zones.hours import Hours
from bot.zones.replay import file_hash, iso_ms

from .config import (
    BATCH_ENTRY_POLICY,
    CLOSE_ONLY,
    LADDER,
    RETIRED_LIMITS,
    TARGET_POLICY,
)
from .entry_clock import QUOTE_POLICY, check_quote
from .ladder import Protection, propose
from .orders import Block, Exposure, Grid, count_for, make_plan, select_labels
from .signal import ENTRY_AUDIT_FIELDS, Hull, Reading
from .structure import ENTRY_POLICY


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
    def __init__(
        self, p, lim, grid, cash, hours, initial, *, exit_opposite, exit_policy=LADDER
    ):
        self.exit_opposite = exit_opposite
        self.exit_policy = exit_policy
        self.groups = (
            Counter()
        )  # identical, independent tickets sharing confirmed levels
        self.amendments = 0
        self.p, self.lim, self.grid, self.cash, self.hours = p, lim, grid, cash, hours
        self.eq = initial
        self.pos = None
        self.last_exit_ms = {1: -1, -1: -1}
        self.consumed_close_s = -1
        self.rows = []
        self.count = Counter()
        self.labels = Counter()
        self.source_conflicts = Counter()
        self.qualified = Counter()
        self.structure_vetoes = Counter()

    def _exit_group(self, why, ms, px, levels, n):
        d, p, _, stamp = self.pos
        self.last_exit_ms[p.side] = ms
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
                "initial_sl": p.sl,
                "initial_tp": p.tp,
                "configured_target_usd": p.configured_target_usd,
                "entry_spread_usd": p.entry_spread_usd,
                "initial_goal_usd": p.goal_usd,
                "ladder_rung": levels.rung,
                "sl": levels.sl,
                "tp": levels.tp,
                "risk_usd_batch": round(p.risk_usd * n, 8),
                "exit_reason": why,
                "exit_price": px,
                "net_usd_per_leg": round(per, 8),
                "net_usd_batch": round(net, 8),
            }
        )

    def close(self, why, ms, px):
        for levels, n in self.groups.items():
            self._exit_group(why, ms, px, levels, n)
        self.groups.clear()
        self.pos = None

    def barriers(self, m, lo, hi):
        if not self.pos or lo >= hi:
            return
        _, p, _, _ = self.pos
        price = m.bid if p.side == 1 else m.ask
        for j in range(lo, hi):
            ms, mark = int(m.ms[j]), float(price[j])
            after = Counter()
            for old, n in self.groups.items():
                # Only levels attached *before* this tick can fill on it. Never
                # retroactively lift a TP already crossed or fill a stop at its
                # theoretical dollar floor through a gap.
                if p.side * (mark - old.sl) <= 0:
                    self._exit_group(
                        "ladder_stop" if old.rung else "stop", ms, mark, old, n
                    )
                    continue
                if p.side * (mark - old.tp) >= 0:
                    self._exit_group("target", ms, old.tp, old, n)
                    continue
                new = None
                if self.exit_policy == LADDER:
                    try:
                        new = propose(
                            self.grid,
                            p.side,
                            p.lots,
                            p.entry,
                            p.goal_usd,
                            old,
                            mark,
                            self.cash,
                        )
                    except Block:
                        pass  # keep the prior protective pair when new levels cannot fit
                if new is None:
                    after[old] += n
                else:
                    after[new] += n
                    self.amendments += n
            self.groups = after
            if not after:
                self.pos = None
                return

    def step(self, m, ds):
        if any(r.trigger for r in ds):
            raise ValueError(
                "Closed-label benchmark cannot simulate live Hull triggers"
            )
        ms = int(m.ms[0])
        now = ms / 1000
        bid = float(m.bid[0])
        ask = float(m.ask[0])
        self.barriers(m, 0, 1)
        self.source_conflicts.update(f"M{v.tf}" for v in ds if v.source_conflict)
        labels = [v for v in ds if v.label]
        self.labels.update(
            f"M{d.tf}:{'BUY' if d.label == 1 else 'SELL'}" for d in labels
        )
        if labels:
            for v in labels:
                if v.entry_side:
                    self.qualified[
                        f"M{v.tf}:{'BUY' if v.entry_side == 1 else 'SELL'}"
                    ] += 1
                elif v.side:
                    self.structure_vetoes[v.entry_reason] += 1
            original, d, why = select_labels(labels)
            # Mirror the user's existing terminal exit, independently of permission
            # to open the replacement. Historical account contains only owned legs.
            if (
                self.exit_opposite
                and original
                and self.pos
                and self.pos[1].side != original.side
            ):
                self.close(
                    "opposite_hull_label", ms, bid if self.pos[1].side == 1 else ask
                )
            if d:
                # The ordered feed starts each minute with its first actual tick.
                # Use the historic tick clock, never today's wall time or a bar-open fill.
                timing = check_quote(d.close_s, now, self.lim, ms, bid, ask)
                duplicate = self.consumed_close_s >= d.close_s
                self.consumed_close_s = max(self.consumed_close_s, d.close_s)
                if duplicate:
                    why = "already_consumed"
                elif self.pos and self.pos[1].side == d.side:
                    why = "same_side_batch_open"
                elif self.pos:
                    why = "symbol_busy"
                elif self.last_exit_ms[d.side] >= d.close_s * 1000:
                    why = "same_side_batch_at_signal_close"
                elif timing.reason != "ready":
                    why = timing.reason
                elif not math.isfinite(self.eq) or self.eq <= 0:
                    # When flat, realized equity is the entire account equity.
                    why = "invalid_equity"
                else:
                    why = (
                        self.hours.status(now)[0]
                        if self.p.session == "configured"
                        else ""
                    )
                if not why:
                    try:
                        p = make_plan(d, self.p, self.grid, bid, ask, self.cash)
                        n = count_for(self.p.rate, self.lim, Exposure(), p)
                        self.pos = (d, p, n, ms)
                        self.groups = Counter({Protection(0, p.sl, p.tp): n})
                        why = "opened"
                    except Block as exc:
                        why = str(exc)
            self.count[why] += 1
        self.barriers(m, 0 if self.pos and self.pos[3] == ms else 1, len(m.ms))

    def finish(self, last):
        if self.pos:
            p = self.pos[1]
            self.close(
                "sample_end",
                int(last.ms[-1]),
                float(last.bid[-1] if p.side == 1 else last.ask[-1]),
            )
        # Count each signal batch once, retaining the per-group exit ledger.
        totals = Counter()
        for r in self.rows:
            totals[(r["signal"], r["tf"], r["entry_time"])] += r["net_usd_batch"]
        vals = list(totals.values())
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
            closed_groups=len(self.rows),
            ladder_ticket_amendments=self.amendments,
            wins=sum(v > 0 for v in vals),
            positions=sum(r["legs"] for r in self.rows),
            exits=dict(Counter(r["exit_reason"] for r in self.rows)),
            label_counts=dict(self.labels),
            source_conflicts=dict(self.source_conflicts),
            entry_qualified_at_close=dict(self.qualified),
            structure_vetoes_at_close=dict(self.structure_vetoes),
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
    "initial_sl",
    "initial_tp",
    "configured_target_usd",
    "entry_spread_usd",
    "initial_goal_usd",
    "ladder_rung",
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
            exit_opposite=h.indicator.exit_opposite,
            exit_policy=h.exit_policy,
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
                row = asdict(d)
                row["structure"] = json.dumps(row["structure"], separators=(",", ":"))
                labels.append(
                    {"closed_utc": iso_ms(d.close_s * 1000), **row, **d.entry_audit()}
                )
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
        "entry_policy": ENTRY_POLICY,
        "signal_policy": h.indicator.signal_policy,
        "batch_entry_policy": BATCH_ENTRY_POLICY,
        "entry_timing": CLOSE_ONLY,
        "configured_scanner_entry_timing": h.entry_timing,
        "entry_quote_policy": QUOTE_POLICY,
        "new_ticket_exit_policy": h.exit_policy,
        "new_ticket_target_policy": TARGET_POLICY,
        "configured_target_usd": p.target_usd,
        "profile": p.model_dump(),
        "indicator": h.indicator.model_dump(),
        "limits": h.limits.model_dump(),
        "retired_limits": list(RETIRED_LIMITS),
        "contract_size": contract,
        "tick_size": unit,
        "conversion": conversion,
        "start_utc": iso_ms(lo * 1000),
        "end_exclusive_utc": iso_ms(hi * 1000),
        "note": "Closed Hull crosses in confirmed BOS half-ranges only; no CHoCH entry, retest order or intrabar signal. USD, spread only. Actual crossing quote for SL; TP at limit. No peak-equity halt or per-minute send cap. Ladder checks prior barriers first on each ordered quote. Identical initial fills; no amendment latency, dealer rejection, commission, broker margin/volume checks or guarantee of atomic fills.",
    }
    with (out / "labels.csv").open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            ["closed_utc", *Reading.__dataclass_fields__, *ENTRY_AUDIT_FIELDS],
            lineterminator="\n",
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
