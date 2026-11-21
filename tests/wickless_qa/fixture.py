"""Create deterministic integer-tick candles & expected close decisions for PineTS."""

import json
import random
import sys
from pathlib import Path

from bot.wickless.config import Rules
from bot.wickless.engine import TF, Bar, Engine


def fixture(seed=71):
    rand = random.Random(seed)
    px = 10000
    t = 1704067200
    bars = []
    for i in range(1000):
        if i == 560:
            t += 86400
        o = px + rand.randint(-5, 5)
        drift = 1 if (i // 80) % 2 else -1
        c = o + rand.randint(-22, 22) + drift * 5
        h = max(o, c) + (rand.randint(1, 9) if i % 3 else 0)
        l = min(o, c) - (rand.randint(1, 9) if i % 4 else 0)
        bars.append([t, o, h, l, c])
        px = c
        t += TF
    return bars


def export(bars, opts=None):
    eng = Engine(Rules(**(opts or {})), 30)
    rows = []
    for bar in bars:
        o = eng.update(Bar(*bar))
        s = o.signal
        rows.append(
            [
                o.trend,
                o.trend * (2 if o.event == "mss" else 1) if o.event else 0,
                None if eng.structure.prot is None else eng.structure.prot.v,
                o.raw,
                o.armed.side if o.armed else 0,
                o.armed.sl if o.armed else None,
                len(eng.pending),
                0 if s is None else s.zone.side,
                None if s is None else s.zone.index,
                None if s is None else s.zone.sl,
                None if s is None else s.tp,
            ]
        )
    return rows


if __name__ == "__main__":
    bars = fixture()
    mirror = [[t, 20000 - o, 20000 - l, 20000 - h, 20000 - c] for t, o, h, l, c in bars]
    Path(sys.argv[1]).write_text(
        json.dumps(
            [
                {"bars": b, "rows": export(b, opts), "opts": pine}
                for b, opts, pine in [
                    (bars, {}, {}),
                    (mirror, {}, {}),
                    (
                        bars,
                        {
                            "tolerance_ticks": 2,
                            "min_body_ticks": 2,
                            "stop_buffer_ticks": 2,
                            "retest_bars": 5,
                        },
                        {"tol": 2, "minBody": 2, "buf": 2, "ttl": 5},
                    ),
                ]
            ]
        )
    )
