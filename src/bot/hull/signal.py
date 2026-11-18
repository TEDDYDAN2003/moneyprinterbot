"""Port of the supplied Free Chart Hull calculation. Only accept FINISHED OHLC bars."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from bot.zones.engine import Bar

from .config import FRAMES, Indicator


class WMA:
    def __init__(self, n):
        self.q = deque(maxlen=n)
        self.n = n
        self.div = n * (n + 1) / 2

    def push(self, v):
        if v is None:
            self.q.clear()
            return None
        self.q.append(v)
        if len(self.q) == self.n:
            # Match ordinary JS multiply/add ordering even on Python versions with compensated sum().
            weighted = 0.0
            for i, x in enumerate(self.q, 1):
                weighted += i * x
            return weighted / self.div
        return None


@dataclass(frozen=True, slots=True)
class Reading:
    close_s: int
    tf: int
    close: float
    a: float | None
    b: float | None
    ema: float
    atr: float | None
    label: int
    side: int
    reason: str
    anchor: float | None
    anchor_s: int | None
    raw_sl: float | None
    window_start_s: int | None
    prior_bars: int
    pivot_low: float | None
    pivot_high: float | None


class Hull:
    def __init__(self, tf: int, p: Indicator, pip_size: float):
        if tf not in FRAMES:
            raise ValueError("Hull timeframes are M1, M2, M5 only")
        self.tf, self.p, self.pip = tf, p, pip_size
        n = p.hull_len
        h = max(1, n // 2)
        self.weights = [
            WMA(z)
            for z in (
                h,
                n,
                max(1, math.floor(math.sqrt(n) + 0.5)),
                max(1, h // 3),
                max(1, h // 2),
                h,
                h,
            )
        ]
        self.prev = None
        self.last_a = self.last_b = self.atr = self.ema = None
        self.n = 0
        self.tr_sum = 0.0
        self.hist = deque(maxlen=max(2 * p.swing_len + 1, p.pullback_bars + 1))
        self.low = self.high = None
        self.last_cross = {1: None, -1: None}

    def update(self, c: Bar) -> Reading:
        if self.prev and c.t <= self.prev.t:
            raise ValueError("Closed bars must be unique and ordered")
        p = self.p
        w = self.weights
        values = {
            "open": c.o,
            "high": c.h,
            "low": c.l,
            "close": c.c,
            "hl2": (c.h + c.l) / 2,
            "hlc3": (c.h + c.l + c.c) / 3,
            "ohlc4": (c.o + c.h + c.l + c.c) / 4,
        }
        v = values[p.source]
        x, y = w[0].push(v), w[1].push(v)
        a = w[2].push(2 * x - y if x is not None and y is not None else None)
        x, y, z = [f.push(c.c) for f in w[3:6]]
        b = w[6].push(3 * x - y - z if None not in (x, y, z) else None)
        label = 0
        if None not in (a, b, self.last_a, self.last_b):
            if b > a and self.last_b < self.last_a:
                label = 1
            if a > b and self.last_a < self.last_b:
                label = -1
        self.ema = (
            c.c
            if self.ema is None
            else self.ema + 2 / (p.ema_length + 1) * (c.c - self.ema)
        )
        tr = (
            c.h - c.l
            if self.prev is None
            else max(c.h - c.l, abs(c.h - self.prev.c), abs(c.l - self.prev.c))
        )
        self.n += 1
        if self.atr is None:
            self.tr_sum += tr
            if self.n == p.atr_length:
                self.atr = self.tr_sum / p.atr_length
        else:
            self.atr = (self.atr * (p.atr_length - 1) + tr) / p.atr_length
        prior = list(self.hist)[-p.pullback_bars :] if label else []
        self.hist.append(c)
        s = p.swing_len
        if len(self.hist) >= 2 * s + 1:
            span = list(self.hist)[-(2 * s + 1) :]
            mid = span[s]
            left = span[:s]
            right = span[s + 1 :]
            if all(mid.l <= q.l for q in left) and all(mid.l < q.l for q in right):
                self.low = (mid.l, mid.t)
            if all(mid.h >= q.h for q in left) and all(mid.h > q.h for q in right):
                self.high = (mid.h, mid.t)
        anchor = when = sl = window = None
        count = 0
        side = 0
        reason = "no_cross"
        if label:
            if p.stop == "local_pullback":
                since = self.last_cross[-label]
                prior = [q for q in prior if since is None or q.t >= since]
                if prior:
                    # Most recent equal wick wins. Signal candle is deliberately excluded.
                    pick = (
                        min(prior, key=lambda q: (q.l, -q.t))
                        if label == 1
                        else max(prior, key=lambda q: (q.h, q.t))
                    )
                    anchor, when = (pick.l if label == 1 else pick.h), pick.t
                    window = prior[0].t
                    count = len(prior)
            else:
                chosen = self.low if label == 1 else self.high
                if chosen:
                    anchor, when = chosen
            if anchor is not None and self.atr is not None:
                sl = anchor - label * (
                    p.atr_buffer * self.atr + p.extra_pips * self.pip
                )
            if sl is None:
                reason = "no_known_swing_or_atr"
            elif sl <= 0 or label * (c.c - sl) <= 0:
                reason = "swing_already_lost_at_close"
            elif p.signal == "ema_plan" and label * (c.c - self.ema) <= 0:
                reason = "ema_filter"
            else:
                side = label
                reason = "ready"
            self.last_cross[label] = c.t
        self.prev, self.last_a, self.last_b = c, a, b
        return Reading(
            c.t + self.tf * 60,
            self.tf,
            c.c,
            a,
            b,
            self.ema,
            self.atr,
            label,
            side,
            reason,
            anchor,
            when,
            sl,
            window,
            count,
            self.low[0] if self.low else None,
            self.high[0] if self.high else None,
        )
