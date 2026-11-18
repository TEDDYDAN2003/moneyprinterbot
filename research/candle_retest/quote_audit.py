"""Post-trade audit of short stop events. These measurements NEVER feed the rule."""

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

import pandas as pd

from bot.zones.feed import tick_minutes
from bot.zones.replay import file_hash


def stamp(s):
    return pd.Timestamp(s).value // 1_000_000


def audit(cache, evidence, out):
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    pending = defaultdict(list)
    triggers = defaultdict(list)
    for p in sorted((evidence / "followup").glob("*/reaction_managed.csv")):
        with p.open() as f:
            for r in csv.DictReader(f):
                if r["side"] != "SELL" or r["exit"] != "stop":
                    continue
                u, v = stamp(r["entry_utc"]), stamp(r["exit_utc"])
                i = len(rows)
                r.update(window=p.parent.name, u=u, v=v, ticks_held=0)
                rows.append(r)
                triggers[stamp(r["signal_utc"]) // 1000 - 60].append(i)
                for k in range(u // 60000 * 60, v // 60000 * 60 + 1, 60):
                    pending[k].append(i)
    ex_start = stamp("2026-11-11T12:38:00Z") // 1000
    ex_end = stamp("2026-11-11T12:53:00Z") // 1000
    bars = []
    path = []
    inputs = json.loads(Path(__file__).with_name("inputs.json").read_text())
    files = [
        cache / f
        for f, h in sorted(inputs["sha256"].items())
        if file_hash(cache / f) == h
    ]
    if len(files) != len(inputs["sha256"]):
        raise ValueError("Bad input checksum")
    for m in tick_minutes(files):
        t = m.bar.t
        if ex_start <= t < ex_end:
            bars.append([t, m.bar.o, m.bar.h, m.bar.l, m.bar.c])
        for i in triggers.get(t, ()):
            rows[i]["reaction_ask_high"] = float(m.ask.max())
        for i in pending.get(t, ()):
            r = rows[i]
            u, v = r["u"], r["v"]
            mask = (m.ms >= u) & (m.ms <= v)
            if not mask.any():
                continue
            ms, b, a = m.ms[mask], m.bid[mask], m.ask[mask]
            # Unambiguous millisecond boundary on this data. Never silently infer event order here.
            if (ms == u).sum() > 1 or (ms == v).sum() > 1:
                raise ValueError("Ambiguous boundary requires quote-index audit")
            r["ticks_held"] += len(b)
            r["max_held_bid"] = max(r.get("max_held_bid", 0), float(b.max()))
            if ms[0] == u:
                assert abs(b[0] - float(r["fill"])) < 1e-8
                r["entry_bid"], r["entry_ask"] = float(b[0]), float(a[0])
                r["entry_spread"] = round(a[0] - b[0], 6)
            if ms[-1] == v:
                r["exit_bid"], r["exit_ask"] = float(b[-1]), float(a[-1])
                r["exit_spread"] = round(a[-1] - b[-1], 6)
                assert a[-1] >= float(r["sl"])
            if r["window"] == "later" and r["setup"] == "sweep_reclaim":
                path += [
                    [int(ts - u), float(bb), float(aa)]
                    for ts, bb, aa in zip(ms, b, a, strict=True)
                ]
    for r in rows:
        r["bid_never_reached_buffered_structure"] = r["max_held_bid"] < float(
            r["stop_bid"]
        )
    fields = [
        "window",
        "signal_utc",
        "entry_utc",
        "exit_utc",
        "source_tf",
        "setup",
        "pattern",
        "reaction_high",
        "reaction_ask_high",
        "invalidation_bid",
        "stop_bid",
        "max_held_bid",
        "entry_bid",
        "entry_ask",
        "entry_spread",
        "sl",
        "exit_bid",
        "exit_ask",
        "exit_spread",
        "ticks_held",
        "risk_usd",
        "net_usd",
        "bid_never_reached_buffered_structure",
    ]
    with (out / "short_stops.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fields, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    (out / "nov11_case.json").write_text(
        json.dumps(
            {
                "case": "November 11, 12:49 UTC M1 BSL sweep/reclaim",
                "candles_t_o_h_l_c": bars,
                "quotes_from_entry_ms_bid_ask": path,
            },
            indent=1,
        )
        + "\n"
    )
    print(
        "Stopped shorts",
        len(rows),
        "without bid invalidation",
        sum(r["bid_never_reached_buffered_structure"] for r in rows),
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, default=Path("data/public_xau_ticks"))
    ap.add_argument(
        "--evidence", type=Path, default=Path("research/candle_retest/evidence")
    )
    ap.add_argument("--out", type=Path, default=Path("research/candle_retest/examples"))
    a = ap.parse_args()
    audit(a.cache, a.evidence, a.out)
