"""Build the missing M45 period on the *same broker* M15/D1 clock as the MQ5 study.

Only three exact consecutive M15 opens within one D1 bucket can close an M45.
The forming prefix is for Hull projection only; a stale tail cannot stand in for a tick.
"""

import itertools
from bisect import bisect_right
from datetime import UTC, datetime
from math import isfinite

BASE = 900
PERIOD = 2700
DAY = 86400


def aggregate(rows, days, forming=False):
    anchors = [int(d["time"]) for d in days]
    if not anchors or any(a >= b for a, b in itertools.pairwise(anchors)):
        return []
    completed, part, key = [], [], None
    last = -1
    for row in rows:
        t = int(row["time"])
        if t <= last:
            return []  # no sorting, guessing or duplicate substitution
        last = t
        j = bisect_right(anchors, t) - 1
        if j < 0 or t - anchors[j] >= DAY or (t - anchors[j]) % BASE:
            part, key = [], None
            continue
        start = anchors[j] + (t - anchors[j]) // PERIOD * PERIOD
        if start != key:
            part, key = [], start
        vals = tuple(float(row[k]) for k in ("open", "high", "low", "close"))
        o, h, l, c = vals
        if (
            t != start + BASE * len(part)
            or not all(map(isfinite, vals))
            or not 0 < l <= min(o, c) <= max(o, c) <= h
        ):
            part = []
            continue
        part.append(vals)
        if not forming and len(part) == 3:
            completed.append(_row(start, part))
    return ([_row(key, part)] if part else []) if forming else completed


def _row(start, part):
    return {
        "time": start,
        "open": part[0][0],
        "high": max(x[1] for x in part),
        "low": min(x[2] for x in part),
        "close": part[-1][3],
        "last_m15": start + (len(part) - 1) * BASE,
    }


def rates45(mt, name, pos, count):
    if pos not in (0, 1) or pos == 0 and count != 1:
        raise ValueError("M45 supports closed history or a single forming snapshot")
    rows = mt.copy_rates_from_pos(
        name, mt.TIMEFRAME_M15, pos, 3 if pos == 0 else 3 * count + 2
    )
    if rows is None or not len(rows):
        return None
    first, end = int(rows[0]["time"]), int(rows[-1]["time"])
    days = mt.copy_rates_range(
        name,
        mt.TIMEFRAME_D1,
        datetime.fromtimestamp(first - DAY, UTC),
        datetime.fromtimestamp(end, UTC),
    )
    if days is None:
        return None
    return aggregate(rows, days, forming=pos == 0)[-count:]
