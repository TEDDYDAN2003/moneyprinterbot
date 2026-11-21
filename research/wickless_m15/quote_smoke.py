"""Deterministic EURUSDm smoke check on two pinned indicative tick archives; zero MT5 I/O.
Sep = structure warm-up only. Oct = fixed rules on ordered bid/ask, one ticket.
Usage: python research/wickless_m15/quote_smoke.py SEPTEMBER.zip OCTOBER.zip OUTPUT_DIR
"""

import csv
import hashlib
import itertools
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd

from bot.hull.orders import Block, Grid
from bot.wickless.config import load
from bot.wickless.engine import TF, Bar, Engine, ticks
from bot.wickless.execution import MAGIC, execute
from bot.wickless.terminal import HISTORY

SYMBOL = "EURUSDm"
STEP = 0.00001
SHA = {
    "09": "9ca940e572c87328ce626e1a42c6f1fe69fbf6e0ab83e83c9a53488fe52ca394",
    "10": "6b6b48b92cff520b9f3d53753299ddcd9f0274214d235048e3952ab9f12e9ad8",
}


def archive(path, month):
    assert hashlib.sha256(path.read_bytes()).hexdigest() == SHA[month]
    data = pd.read_csv(
        path, compression="zip", usecols=["Symbol", "Timestamp", "Bid", "Ask"]
    )
    assert data.Symbol.eq(SYMBOL).all()
    tm = (
        pd.to_datetime(data.Timestamp, format="ISO8601", utc=True)
        .array.as_unit("ms")
        .asi8
    )
    assert (np.diff(tm) >= 0).all()

    def units(col):
        val = data[col].to_numpy() / STEP
        v = np.rint(val)
        assert (abs(v - val) < 1e-6).all()
        return v.astype("int64")

    bid, ask = units("Bid"), units("Ask")
    assert ((bid > 0) & (ask >= bid)).all()
    groups = np.r_[0, np.flatnonzero(np.diff(tm // 900000)) + 1, len(tm)]
    bars = [
        Bar(
            int(tm[a] // 900000) * TF,
            int(bid[a]),
            int(bid[a:b].max()),
            int(bid[a:b].min()),
            int(bid[b - 1]),
        )
        for a, b in itertools.pairwise(groups)
    ]
    return tm, bid, ask, groups, bars


class Tape:
    # Only the read-only adapter surface used by the production admission function.
    ACCOUNT_TRADE_MODE_DEMO = ORDER_TYPE_BUY = SYMBOL_TRADE_MODE_DISABLED = 0
    ORDER_TYPE_SELL = SYMBOL_TRADE_MODE_LONGONLY = 1
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = SYMBOL_TRADE_MODE_SHORTONLY = 2
    SYMBOL_TRADE_MODE_CLOSEONLY = 3
    ORDER_TIME_GTC = ORDER_FILLING_FOK = 0
    TRADE_ACTION_DEAL = 1

    def __init__(self):
        self.a = NS(
            login=1,
            server="Archive",
            connected=True,
            equity=10000,
            currency="USD",
            trade_mode=0,
            margin_mode=2,
        )
        self.info = NS(
            name=SYMBOL,
            point=STEP,
            trade_tick_size=STEP,
            trade_freeze_level=0,
            trade_stops_level=0,
            digits=5,
            volume_min=0.01,
            volume_step=0.01,
            volume_max=100,
            trade_mode=4,
            filling_mode=1,
        )
        self.pos = []

    def account_info(self):
        return self.a

    def terminal_info(self):
        return self.a

    def positions_get(self):
        return self.pos

    def orders_get(self):
        return []

    def symbol_info(self, name):
        assert name == SYMBOL
        return self.info

    def symbol_info_tick(self, name):
        assert name == SYMBOL
        return self.q

    def order_calc_profit(self, typ, name, lots, e, x):
        return (1 if typ == 0 else -1) * (x - e) * 100000 * lots


def main(sep, october, folder):
    cfg = load(Path(__file__).resolve().parents[2] / "wickless.example.json")
    p = cfg.pairs[0]
    r = cfg.rules
    st, _, _, _, past = archive(sep, "09")
    tm, bid, ask, grp, bars = archive(october, "10")
    assert len(st) == 820806 and len(tm) == 1038972 and st[-1] < tm[0]
    n = len(past)
    history = past + bars

    def warm(end):
        e = Engine(r, p.zone_ticks(r, STEP))
        for b in history[end - HISTORY : end]:
            e.update(b)
        e.pending.clear()
        return e

    e = warm(n)
    mt = Tape()
    g = Grid.from_info(mt.info)
    held = None
    ledger = []
    counts = Counter()

    def exits(a, b):
        nonlocal held
        if held is None or a >= b:
            return
        d, ent, sl, tp, row = held
        px = (bid if d == 1 else ask)[a:b]
        hit = np.flatnonzero((d * (px - sl) <= 0) | (d * (px - tp) >= 0))
        if not len(hit):
            return
        k = a + int(hit[0])
        mark = int(px[hit[0]])
        win = d * (mark - tp) >= 0
        x = tp if win else mark  # no price improvement; adverse stop gaps visible
        row.update(
            exit_ms=int(tm[k]),
            exit=round(x * STEP, 5),
            outcome="TP" if win else "SL",
            R=(x - ent) / (ent - sl),
            pnl_usd=round(d * (x - ent) * STEP * 100000 * p.lots, 6),
        )
        mt.pos.clear()
        held = None

    for j, (a, b) in enumerate(itertools.pairwise(grp)):
        exits(
            a, a + 1
        )  # existing attached SL/TP is checked before a new deal at this quote
        if j > 0:
            prev = bars[j - 1]
            out = None
            if prev.t != e.last + TF:
                e = warm(n + j)
                counts["rewarmed_after_gap"] += 1
            else:
                out = e.update(prev)
                counts["setups"] += bool(out.armed)
            if out and out.signal:
                sig = out.signal
                stamp = int(tm[a])
                d = sig.zone.side
                mt.q = NS(
                    time_msc=stamp,
                    bid=round(int(bid[a]) * STEP, 5),
                    ask=round(int(ask[a]) * STEP, 5),
                )
                try:
                    plan = execute(
                        mt,
                        cfg,
                        p,
                        g,
                        sig,
                        (1, "Archive"),
                        armed=False,
                        book=None,
                        now=lambda stamp=stamp: stamp / 1000,
                        kill=Path(__file__).parent / "NO_ORDERS_IN_OFFLINE_SMOKE",
                    )
                except Block as exc:
                    counts[str(exc)] += 1
                else:
                    assert held is None
                    ent, sl, tp = (ticks(plan[k], STEP) for k in ("entry", "sl", "tp"))
                    assert (
                        tp - ent == ent - sl
                        and d * (tp - ent) > 0
                        and 0 <= stamp - sig.close_s * 1000 <= 8000
                    )
                    row = {
                        "side": "BUY" if d == 1 else "SELL",
                        "setup_open": sig.zone.born,
                        "close_s": sig.close_s,
                        "quote_ms": stamp,
                        "entry": plan["entry"],
                        "SL": plan["sl"],
                        "TP": plan["tp"],
                        "exit_ms": "",
                        "exit": "",
                        "outcome": "open",
                        "R": "",
                        "pnl_usd": "",
                    }
                    ledger.append(row)
                    held = d, ent, sl, tp, row
                    mt.pos = [
                        NS(symbol=p.symbol, magic=MAGIC, sl=plan["sl"], tp=plan["tp"])
                    ]
                counts["suggestions"] += 1
        exits(a + 1, b)
    wins = [v for v in ledger if v["outcome"] == "TP"]
    losses = [v for v in ledger if v["outcome"] == "SL"]
    grosswin = sum(v["pnl_usd"] for v in wins)
    grossloss = -sum(v["pnl_usd"] for v in losses)
    result = {
        "symbol": p.symbol,
        "config": cfg.model_dump(),
        "model": "first fresh quote, bid/ask spread included, zero broker fees and latency; SL first crossing executable quote, TP at limit",
        "source_urls": [
            f"https://ticks.ex2archive.com/ticks/{SYMBOL}/2026/{m}/Exness_{SYMBOL}_2026_{m}.zip"
            for m in SHA
        ],
        "archive_sha256": SHA,
        "warm_closed_bars": HISTORY,
        "october_ticks": len(tm),
        "october_observed_M15": len(bars),
        "first_ms": int(tm[0]),
        "last_ms": int(tm[-1]),
        "counts": dict(counts),
        "simulated_fills": len(ledger),
        "TP": len(wins),
        "SL": len(losses),
        "open_at_end": int(held is not None),
        "net_before_fees_usd": round(grosswin - grossloss, 5),
        "sum_realized_R": round(sum(v["R"] for v in wins + losses), 5),
        "profit_factor_usd": round(grosswin / grossloss, 5) if grossloss else None,
    }
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "quote-smoke.json").write_text(json.dumps(result, indent=2) + "\n")
    with (folder / "quote-ledger.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(ledger[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(ledger)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main(*map(Path, sys.argv[1:]))
