"""Offline single-pair M15 reference. Chart-close entries, NOT executable quote fills."""

from __future__ import annotations

import csv
import math
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from .engine import TF, Bar, Engine, ticks


def rows(path, tick):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        for x in r:
            raw = x["time"]
            if raw.isdigit():
                t = int(raw)
            else:
                dt = datetime.fromisoformat(raw)
                if dt.tzinfo is None:
                    raise ValueError("Use timezone-aware UTC times or epoch seconds")
                t = int(dt.timestamp())
            if t + TF > time.time():
                raise ValueError("Forming/future M15 candle in export")
            yield Bar(t, *[ticks(x[k], tick) for k in ("open", "high", "low", "close")])


def replay(bars, pair, rules, tick, output=None):
    if not math.isfinite(tick) or tick <= 0:
        raise ValueError("Invalid tick size")
    eng = Engine(rules, pair.zone_ticks(rules, tick))
    counts = Counter()
    events = Counter()
    held = None
    net = 0.0
    ledger = []
    for b in bars:
        counts["bars"] += 1
        if held:
            s, rec = held
            d, sl, tp = s.zone.side, s.zone.sl, s.tp
            hit_sl = d * (b.wick(-d) - sl) <= 0
            hit_tp = d * (b.wick(d) - tp) >= 0
            if hit_sl or hit_tp:
                # Opening gap is seen first; otherwise unknown dual-touch path loses.
                if d * (b.o - sl) <= 0:
                    exit_px, why = b.o, "gap_stop"
                elif d * (b.o - tp) >= 0:
                    exit_px, why = tp, "target"
                elif hit_sl:
                    exit_px, why = sl, "stop"
                else:
                    exit_px, why = tp, "target"
                rr = (exit_px - s.entry) / (s.entry - sl)
                rec.update(
                    exit_bar=b.t,
                    exit_price=round(exit_px * tick, 12),
                    outcome=why,
                    R=rr,
                )
                counts[why] += 1
                net += rr
                held = None
        o = eng.update(b)
        counts["raw"] += int(bool(o.raw))
        counts["setups"] += int(bool(o.armed))
        events.update(reason for _, reason in o.ended)
        if o.signal:
            counts["suggestions"] += 1
            if not held:
                s = o.signal
                rec = {
                    "side": "BUY" if s.zone.side == 1 else "SELL",
                    "setup_open": s.zone.born,
                    "entry_close": s.close_s,
                    "entry": round(s.entry * tick, 12),
                    "SL": round(s.zone.sl * tick, 12),
                    "TP": round(s.tp * tick, 12),
                    "exit_bar": "",
                    "exit_price": "",
                    "outcome": "open",
                    "R": "",
                }
                held = s, rec
                ledger.append(rec)
            else:
                counts["occupied"] += 1
    if not counts["bars"]:
        raise ValueError("Empty M15 input")
    if output:
        with Path(output).open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                lineterminator="\n",
                fieldnames=[
                    "side",
                    "setup_open",
                    "entry_close",
                    "entry",
                    "SL",
                    "TP",
                    "exit_bar",
                    "exit_price",
                    "outcome",
                    "R",
                ],
            )
            w.writeheader()
            w.writerows(ledger)
    return dict(
        model="M15 close-price simulation; no spread, fees or slippage except gaps on stops",
        symbol=pair.symbol,
        **counts,
        simulated_entries=len(ledger),
        open_at_end=int(held is not None),
        unfilled_zones=len(eng.pending),
        ended_zones=dict(events),
        gross_R=round(net, 5),
    )
