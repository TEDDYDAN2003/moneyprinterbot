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


def aggregate(
    rows,
    days,
    forming=False,
    *,
    base=BASE,
    period=PERIOD,
    bars=3,
    last_key="last_m15",
):
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
        if j < 0 or t - anchors[j] >= DAY or (t - anchors[j]) % base:
            part, key = [], None
            continue
        start = anchors[j] + (t - anchors[j]) // period * period
        if start != key:
            part, key = [], start
        vals = tuple(float(row[k]) for k in ("open", "high", "low", "close"))
        o, h, l, c = vals
        if (
            t != start + base * len(part)
            or not all(map(isfinite, vals))
            or not 0 < l <= min(o, c) <= max(o, c) <= h
        ):
            part = []
            continue
        part.append(vals)
        if not forming and len(part) == bars:
            completed.append(_row(start, part, base=base, last_key=last_key))
    return (
        [_row(key, part, base=base, last_key=last_key)] if part else []
    ) if forming else completed


def _row(start, part, *, base=BASE, last_key="last_m15"):
    return {
        "time": start,
        "open": part[0][0],
        "high": max(x[1] for x in part),
        "low": min(x[2] for x in part),
        "close": part[-1][3],
        last_key: start + (len(part) - 1) * base,
    }


def _rates_from_base(mt, name, pos, count, *, mt_tf, base, period, bars, last_key, label):
    if pos not in (0, 1) or pos == 0 and count != 1:
        raise ValueError(f"{label} supports closed history or a single forming snapshot")
    # MT5 hard limit: copy_rates_from_pos fails at count >= 1000.
    # Request bars*count + (bars - 1) base bars; cap count so request stays <= 999.
    pad = bars - 1
    if pos == 1:
        count = min(count, (999 - pad) // bars)
    rows = mt.copy_rates_from_pos(
        name, mt_tf, pos, bars if pos == 0 else bars * count + pad
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
    return aggregate(
        rows,
        days,
        forming=pos == 0,
        base=base,
        period=period,
        bars=bars,
        last_key=last_key,
    )[-count:]


def rates45(mt, name, pos, count):
    return _rates_from_base(
        mt,
        name,
        pos,
        count,
        mt_tf=mt.TIMEFRAME_M15,
        base=BASE,
        period=PERIOD,
        bars=3,
        last_key="last_m15",
        label="M45",
    )


def rates8(mt, name, pos, count):
    """Build synthetic M8 bars from 2 consecutive native M4 bars anchored to broker D1."""
    return _rates_from_base(
        mt,
        name,
        pos,
        count,
        mt_tf=mt.TIMEFRAME_M4,
        base=240,
        period=480,
        bars=2,
        last_key="last_m4",
        label="M8",
    )
