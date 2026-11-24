"""Read one account's scanner JSONL. Distinguish admission, dispatch, touches and proven fills.

Usage: python tools/report_hull_retest.py path/to/demo.jsonl [--details]
No MT5 import, database writes or order APIs. Audit telemetry, not a backtest or a P/L ledger.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path


def report(lines, *, details=False):
    batches = {}
    active = False
    truncated = False
    for number, line in enumerate(lines, 1):
        if not line.endswith("\n"):
            truncated = True
            break  # producer may be appending the last JSONL record
        if not line.strip():
            continue
        e = json.loads(line)
        event = e.get("event", "")
        if event == "started":
            active = e.get("summary", {}).get("entry_route") == "direct_retest_limit"
            continue
        arb = event == "pair_arbitration" and active and e["outcome"] == "selected"
        decide = event == "label_decision" and active
        if not ((arb or decide) or event.startswith("retest_")):
            continue
        tf = e.get("owner_tf") if arb else e.get("tf")
        stamp = e.get("close_s") if arb or decide else e.get("signal")
        if not tf or not stamp:
            continue
        key = (e.get("scanner_magic"), e["symbol"], stamp, tf)
        b = batches.setdefault(
            key,
            {
                "orders": {},
                "deals": {},
                "selected": False,
                "planned": None,
                "dispatch": None,
                "shadow": None,
                "settled": False,
                "last_submit": None,
                "max_send": None,
                "outcome": None,
            },
        )
        if arb:
            b["selected"] = True
        elif decide:
            b["outcome"] = e["outcome"]
        elif event == "retest_batch_plan":
            b.update(
                planned=e["legs"],
                shadow=e["shadow"],
                lots=Decimal(str(e["lots_per_leg"])),
            )
        elif event == "retest_placement_result":
            if e["status"] == "working":
                leg = e["leg"]
                order = e["order"]
                old = b["orders"].setdefault(leg, order)
                if old != order:
                    raise ValueError(f"Conflicting order for one leg at line {number}")
            b["last_submit"] = max(b["last_submit"] or 0, e["close_to_submit_ms"])
            b["max_send"] = max(b["max_send"] or 0, e["order_send_roundtrip_ms"])
        elif event == "retest_dispatch":
            b.update(dispatch=e["dispatch_ms"], outcome=e["outcome"])
        elif event == "retest_fill_receipt":
            item = (e["leg"], e["order"], Decimal(str(e["fill_lots"])))
            if b["deals"].setdefault(e["deal"], item) != item:
                raise ValueError(f"Conflicting opening deal at line {number}")
        elif event == "retest_batch_settled":
            b.update(settled=True, settlement=e["fill_deals"])
        elif event == "retest_shadow_result":
            b["shadow_result"] = e["outcome"]
    totals = Counter(
        {
            k: 0
            for k in (
                "selected_signals",
                "selected_without_plan",
                "shadow_plans",
                "shadow_observed_touches",
                "demo_plans",
                "demo_planned_legs",
                "acknowledged_orders_in_log",
                "legs_with_fill_receipts",
                "legs_with_full_volume",
                "settled_zero_fill_batches",
                "unsettled_demo_plans",
            )
        }
    )
    rows = []
    for (magic, sym, stamp, tf), b in batches.items():
        totals["selected_signals"] += int(b["selected"])
        totals["selected_without_plan"] += int(b["selected"] and b["planned"] is None)
        if b["shadow"] is True:
            totals["shadow_plans"] += 1
            totals["shadow_observed_touches"] += int(
                b.get("shadow_result") == "observed_quote_touch_not_fill"
            )
        if b["shadow"] is False:
            totals["demo_plans"] += 1
            totals["demo_planned_legs"] += b["planned"]
            totals["unsettled_demo_plans"] += int(not b["settled"])
        totals["acknowledged_orders_in_log"] += len(b["orders"])
        volumes = Counter()
        for leg, order, qty in b["deals"].values():
            if leg in b["orders"] and b["orders"][leg] != order:
                raise ValueError("Placement and fill order IDs disagree")
            volumes[leg] += qty
        full = sum(qty == b.get("lots") for qty in volumes.values())
        totals["legs_with_fill_receipts"] += len(volumes)
        totals["legs_with_full_volume"] += full
        totals["settled_zero_fill_batches"] += int(
            b["settled"] and not b["settlement"] and not volumes
        )
        if details:
            rows.append(
                dict(
                    magic=magic,
                    symbol=sym,
                    signal_close_s=stamp,
                    parent_tf=tf,
                    selected=b["selected"],
                    planned=b["planned"],
                    acknowledged=len(b["orders"]),
                    legs_with_fill_receipt=len(volumes),
                    legs_at_full_volume=full,
                    close_to_last_submit_ms=b["last_submit"],
                    max_order_send_roundtrip_ms=b["max_send"],
                    dispatch_ms=b["dispatch"],
                    settled=b["settled"],
                    shadow=b["shadow"],
                    outcome=b.get("shadow_result") or b["outcome"],
                )
            )
    return {
        "counts": dict(totals),
        "ignored_unfinished_last_line": truncated,
        "note": "One account log. Missing events stay unknown. Touches and acknowledgements are not fills; use journal closed broker deals for net P/L.",
        **({"signals": rows} if details else {}),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("log", type=Path)
    p.add_argument("--details", action="store_true")
    args = p.parse_args()
    with args.log.open(encoding="utf-8-sig") as f:
        print(json.dumps(report(f, details=args.details), indent=2))
