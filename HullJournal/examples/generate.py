"""Rebuild the clearly labeled EXAMPLE CSVs (no MT5 dependency or connection)."""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / "tests")]
from fixtures import FakeMT, deal, sample
from ledger import MAGIC, csv_bytes, db_open, identity, make_views, sync


def current_examples():
    """Three illustrative, synthetic cross-candle receipts. No strategy return is implied."""
    ds, events = [], []
    stamp = int(datetime(2026, 11, 17, 15, tzinfo=UTC).timestamp())
    symbol = "XAUUSDm"
    for tf, sign in ((15, 1), (30, -1), (45, 1)):
        typ = int(sign < 0)
        events.append(
            {
                "event": "closed_label",
                "symbol": symbol,
                "tf": tf,
                "close_s": stamp,
                "entry_side": sign,
                "entry_setup": "trend_hull_cross",
            }
        )
        for leg in (1, 2, 3):
            n = 100000 + tf * 10 + leg
            send_ms = stamp * 1000 + 120 + 25 * leg
            bid = round(2000 + leg * 0.01, 2)
            ask = round(bid + 0.10, 2)
            fill = ask if sign == 1 else bid
            wick = 1999.5 if sign == 1 else 2000.5
            sl = round(wick - sign * 0.10, 2)
            tp = round(fill + sign * 1.1, 2)
            comment = f"h{tf}_{stamp}_{leg}"
            events.append(
                {
                    "event": "demo_result",
                    "symbol": symbol,
                    "scanner_magic": MAGIC,
                    "tf": tf,
                    "leg": leg,
                    "signal": stamp,
                    "deal": n,
                    "retcode": 10009,
                    "trigger_msc": stamp * 1000,
                    "entry_side": sign,
                    "entry_timing": "confirmed_close",
                    "initial_stop_policy": "confirmed_cross_candle_wick_v1",
                    "cross_candle_open_s": stamp - tf * 60,
                    "cross_candle_wick": wick,
                    "frozen_cross_sl": sl,
                    "send_tick_time_msc": send_ms,
                    "send_bid": bid,
                    "send_ask": ask,
                    "mss_skip_exception": "failed_counter_mss_return"
                    if tf == 30
                    else None,
                    "request": {
                        "symbol": symbol,
                        "magic": MAGIC,
                        "type": typ,
                        "comment": comment,
                        "sl": sl,
                        "tp": tp,
                    },
                }
            )
            end = sl if tf == 45 else tp
            ds.extend(
                [
                    deal(
                        n,
                        n,
                        stamp,
                        typ,
                        0,
                        0.1,
                        fill,
                        sy=symbol,
                        comment=comment,
                        ms=send_ms % 1000 + 10,
                        commission=-0.10,
                    ),
                    deal(
                        n + 100000,
                        n,
                        stamp + 300 + tf + leg,
                        1 - typ,
                        1,
                        0.1,
                        end,
                        sy=symbol,
                        profit=round(sign * (end - fill) * 10, 2),
                        commission=-0.10,
                        reason=4 if tf == 45 else 5,
                    ),
                ]
            )
    return ds, events


def generate():
    ds, models = sample()
    current, events = current_examples()
    ds.extend(current)
    mt = FakeMT(ds, max(d.time for d in ds) + 80)
    c = db_open(":memory:")
    with TemporaryDirectory() as folder:
        path = (
            Path(folder)
            / "reports/hull_runtime"
            / identity(mt, MAGIC)["key"]
            / "demo.jsonl"
        )
        path.parent.mkdir(parents=True)
        path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf8")
        sync(c, mt, mt.at, repo=folder)
    with c:
        c.executemany(
            "INSERT INTO labels VALUES(?,?,?,?,?)", [(*k, v) for k, v in models.items()]
        )
        c.executemany(
            "INSERT INTO exit_notes VALUES(?,?,?,?)",
            [
                (x.symbol, str(x.position_id), x.time_msc + 280, "opposite_hull_label")
                for x in ds
                if x.comment == "hull_exit"
            ],
        )
    rows, _, _ = make_views(c)
    c.close()
    for k, rs in rows.items():
        for r in rs:
            r["account_key"] = "SAMPLE"
        (root / "examples" / (k + ".csv")).write_bytes(csv_bytes(rs))
    return rows


if __name__ == "__main__":
    rows = generate()
    print({k: len(v) for k, v in rows.items()})
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(
            json.dumps(rows, separators=(",", ":")), encoding="utf8"
        )
