"""Rebuild the clearly labeled EXAMPLE CSVs (no MT5 dependency or connection)."""

import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / "tests")]
from fixtures import FakeMT, sample

from ledger import csv_bytes, db_open, make_views, sync


def generate():
    ds, models = sample()
    mt = FakeMT(ds, max(d.time for d in ds) + 80)
    c = db_open(":memory:")
    sync(c, mt, mt.at)
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
