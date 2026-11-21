"""Aggregate ONLY complete 15-row UTC quarters from the previous broker M1 export."""

import csv
import hashlib
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path


def run(src, dst):
    buckets = defaultdict(list)
    last = -1
    with src.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            dt = datetime.fromisoformat(r["time"])
            assert dt.tzinfo is not None
            t = int(dt.timestamp())
            assert t % 60 == 0 and t > last
            last = t
            o, h, l, c = (Decimal(r[k]) for k in ("open", "high", "low", "close"))
            assert 0 < l <= min(o, c) <= max(o, c) <= h
            buckets[t // 900 * 900].append((t, o, h, l, c))
    written = 0
    with dst.open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(("time", "open", "high", "low", "close"))
        for t, items in buckets.items():
            if len(items) != 15:
                continue
            assert [x[0] for x in items] == list(range(t, t + 900, 60))
            w.writerow(
                (
                    datetime.fromtimestamp(t, UTC).isoformat(),
                    items[0][1],
                    max(x[2] for x in items),
                    min(x[3] for x in items),
                    items[-1][4],
                )
            )
            written += 1
    return {
        "input": src.name,
        "input_sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
        "minutes": sum(map(len, buckets.values())),
        "complete_quarters": written,
        "omitted_incomplete_nonempty_quarters": len(buckets) - written,
        "m15_sha256": hashlib.sha256(dst.read_bytes()).hexdigest(),
    }


if __name__ == "__main__":
    print(json.dumps(run(Path(sys.argv[1]), Path(sys.argv[2])), indent=2))
