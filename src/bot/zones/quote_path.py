"""Read-only, bounded MT5 quote history for software watches. Never fill a past touch."""

from datetime import UTC, datetime

import numpy as np


def quotes_to_snapshot(mt, name, start_ms, tick):
    end = int(tick.time_msc)
    if start_ms > end:
        return None
    # MT5 datetime resolution can be whole seconds. Slice by integer ms AFTER querying.
    raw = mt.copy_ticks_range(
        name,
        datetime.fromtimestamp(start_ms // 1000 - 10, UTC),
        datetime.fromtimestamp(end // 1000 + 1, UTC),
        mt.COPY_TICKS_ALL,
    )
    if raw is None or not len(raw):
        return None
    ms = np.asarray(raw["time_msc"], dtype="int64")
    if (np.diff(ms) < 0).any() or not (ms <= start_ms).any():
        return None  # No overlap: do not silently trust a truncated terminal history.
    mask = (ms >= start_ms) & (ms <= end)
    ms = ms[mask]
    b = np.asarray(raw["bid"], dtype=float)[mask]
    a = np.asarray(raw["ask"], dtype=float)[mask]
    if (
        not len(ms)
        or (np.diff(ms) < 0).any()
        or not np.isfinite(b).all()
        or not np.isfinite(a).all()
        or (b <= 0).any()
        or (a < b).any()
    ):
        return None
    if ms[-1] != end or (b[-1], a[-1]) != (tick.bid, tick.ask):
        return None  # Snapshot absent, or same-millisecond history already moved.
    return ms, b, a


def path_cancel(w, bids):
    side = w.d.side
    bad = side * (bids - w.invalid_bid) <= 0
    if w.d.objective is not None:
        bad |= side * (bids - w.d.objective) >= 0
    ix = np.flatnonzero(bad)
    return w.crossed(bids[ix[0]]) if len(ix) else ""
