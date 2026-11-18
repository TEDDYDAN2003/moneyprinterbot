"""One incremental, causal M1 decision clock per instrument (bid OHLC).

A zone is a hypothesis about liquidity, NOT a measurement of resting orders.
A wick does not arm a BOS. Levels confirmed on this close are eligible next bar.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from math import ceil, floor, isfinite

from .candles import patterns
from .config import Instrument, Rules


@dataclass(frozen=True, slots=True)
class Bar:
    t: int  # UTC minute OPEN in Unix seconds
    o: float
    h: float
    l: float
    c: float

    def __post_init__(self):
        if (
            self.t % 60
            or not all(isfinite(x) for x in (self.o, self.h, self.l, self.c))
            or not (0 < self.l <= min(self.o, self.c) <= max(self.o, self.c) <= self.h)
        ):
            raise ValueError("Invalid bid M1 bar or non-minute timestamp")


@dataclass(slots=True)
class Pool:
    key: str
    tf: int
    price: float
    w: float
    role: int  # +1 support (sell-side stops below), -1 resistance (buy-side above)
    born: int  # confirmation close, not pivot time
    expires: int
    label: str
    touches: int = 1
    retest: str = ""
    break_time: int = 0
    pierced: bool = False
    excursion: float | None = None
    used: bool = False


@dataclass(frozen=True, slots=True)
class Decision:
    t: int  # time the decision became known (bar CLOSE)
    reason: str
    side: int = 0  # +1 buy, -1 sell, 0 observe
    tf: int = 0
    zone_id: str = ""
    level: float = 0.0
    width: float = 0.0
    invalidation: float = 0.0  # bid-chart wick / far side of zone
    objective: float | None = None  # nearest opposing bid-chart zone edge
    atr: float = 0.0
    momentum: float = 0.0
    # (zone id, lower edge, upper edge) for ALL touched zones, including WAIT.
    observed_levels: tuple[tuple[str, float, float], ...] = ()
    swing: str = ""
    pool: str = ""
    touches: int = 0
    line_confluence: bool = False
    trends: tuple[int, int, int] = (0, 0, 0)
    breaks: tuple[
        tuple[int, int], ...
    ] = ()  # (source minutes, BOS direction) this close
    reactions: tuple[
        str, ...
    ] = ()  # all interactions, even if another level wins priority

    candle: tuple[str, ...] = ()
    previous_candle: tuple[str, ...] = ()
    source_candle: tuple[str, ...] = ()
    prior_at_zone: bool = False
    trigger_high: float = 0.0
    trigger_low: float = 0.0
    prior_momentum: float = 0.0
    bar_direction: int = 0


@dataclass
class Frame:
    n: int
    strength: int
    chunk: list[Bar] = field(default_factory=list)
    tr: deque = field(default_factory=lambda: deque(maxlen=14))
    pivots: dict = field(
        default_factory=lambda: {1: deque(maxlen=2), -1: deque(maxlen=2)}
    )
    last_close: float | None = None
    trend: int = 0

    def __post_init__(self):
        self.window = deque(maxlen=2 * self.strength + 1)
        self.latest_key = {1: "", -1: ""}

    @property
    def atr(self):
        return sum(self.tr) / 14 if len(self.tr) == 14 else 0.0

    def ingest(self, bar: Bar):
        start = bar.t // (60 * self.n) * (60 * self.n)
        if self.chunk and (self.chunk[0].t != start or self.chunk[-1].t + 60 != bar.t):
            self.chunk.clear()
        if not self.chunk and bar.t != start:
            return None
        self.chunk.append(bar)
        if len(self.chunk) != self.n:
            return None
        b = Bar(
            start,
            self.chunk[0].o,
            max(x.h for x in self.chunk),
            min(x.l for x in self.chunk),
            bar.c,
        )
        self.chunk.clear()
        prev = self.last_close if self.last_close is not None else b.o
        self.tr.append(max(b.h - b.l, abs(b.h - prev), abs(b.l - prev)))
        self.last_close = b.c
        self.window.append(b)
        return b

    def confirmed(self):
        if len(self.window) != self.window.maxlen or self.atr <= 0:
            return
        v = list(self.window)
        mid, left, right = v[self.strength], v[: self.strength], v[self.strength + 1 :]
        # Deterministic first of adjacent equal extremes, not every bar on a plateau.
        for role in (1, -1):
            val = (lambda b: b.l) if role == 1 else (lambda b: -b.h)
            if val(mid) < min(map(val, left)) and val(mid) <= min(map(val, right)):
                p = mid.l if role == 1 else mid.h
                old = self.pivots[role]
                if not old:
                    label = "L" if role == 1 else "H"
                elif p == old[-1][1]:
                    label = "EQL" if role == 1 else "EQH"
                elif role == 1:
                    label = "HL" if p > old[-1][1] else "LL"
                else:
                    label = "HH" if p > old[-1][1] else "LH"
                old.append((mid.t, p))
                yield role, p, label

    def line_near(self, role, t, price, w, max_bars):
        ps = self.pivots[role]
        if len(ps) < 2:
            return False
        (t0, p0), (t1, p1) = ps
        if role * (p1 - p0) <= 0 or t - t1 > max_bars * self.n * 60:
            return False
        projected = p1 + (p1 - p0) * (t - t1) / (t1 - t0)
        return abs(price - projected) <= w


class ZoneEngine:
    def __init__(self, rules: Rules, symbol: Instrument):
        self.r, self.s = rules, symbol
        self.frames = {n: Frame(n, rules.pivot_bars) for n in (1, 5, 15)}
        self.zones: list[Pool] = []
        self.prev: Bar | None = None
        self.last_momentum = 0.0
        self.last_patterns = {n: () for n in self.frames}

    def _add(self, f, role, p, label, t):
        w = max(self.s.tick_size, self.r.width_atr * f.atr)
        z = min(
            (
                z
                for z in self.zones
                if z.tf == f.n
                and z.role == role
                and not z.retest
                and abs(z.price - p) <= min(z.w, w)
            ),
            default=None,
            key=lambda z: abs(z.price - p),
        )
        expiry = t + 60 * f.n * self.r.recency_bars
        if z is None:
            z = Pool(f"M{f.n}:{t}:{role}", f.n, p, w, role, t, expiry, label)
            self.zones.append(z)
        else:
            # New independently confirmed pivot: keep the ORIGINAL level/width, rearm once.
            z.touches += 1
            z.label, z.expires, z.used = label, expiry, False
        f.latest_key[role] = z.key

    def _objective(self, side, price):
        ahead = [
            z.price - side * z.w
            for z in self.zones
            if z.role == -side and not z.pierced and side * (z.price - price) > 0
        ]
        # Already inside an opposing zone? Return current price (no room), don't skip it.
        step = self.s.round_step
        if step:
            ahead.append(
                ((floor(price / step) + 1) if side == 1 else (ceil(price / step) - 1))
                * step
            )
        return (min(ahead) if side == 1 else max(ahead)) if ahead else None

    def update(self, b: Bar) -> Decision:
        if self.prev and b.t <= self.prev.t:
            raise ValueError(
                "Bars must be strictly increasing, one update per completed minute"
            )
        if self.prev and b.t - self.prev.t > 60 * self.r.reset_gap_minutes:
            self.__init__(self.r, self.s)
        if self.prev and b.t != self.prev.t + 60:
            self.zones = [z for z in self.zones if not z.retest and not z.pierced]
            for f in self.frames.values():
                f.chunk.clear()
        old, self.prev = self.prev, b
        t = b.t + 60
        prev_pattern = self.last_patterns[1] if old and old.t + 60 == b.t else ()
        completed = [f for f in self.frames.values() if f.ingest(b) is not None]
        for f in completed:
            prev_tf = (
                f.window[-2]
                if len(f.window) > 1 and f.window[-2].t + f.n * 60 == f.window[-1].t
                else None
            )
            self.last_patterns[f.n] = patterns(f.window[-1], prev_tf, self.s.tick_size)
        closed = {f.n for f in completed}
        atr = self.frames[1].atr
        rng = max(b.h - b.l, self.s.tick_size)
        body = b.c - b.o
        mom = (
            0.4 * body / rng
            + 0.35 * (2 * b.c - b.h - b.l) / rng
            + 0.25 * max(-1.0, min(1.0, body / max(atr, self.s.tick_size)))
        )
        self.zones = [
            z
            for z in self.zones
            if t <= z.expires
            and (not z.retest or t - z.break_time <= self.r.retest_minutes * 60)
        ]
        notes, candidates, breaks = [], [], []
        for z in self.zones:
            side = z.role
            d = side * (b.c - z.price)
            if d < -z.w:
                z.pierced = True
                edge = b.l if side == 1 else b.h
                z.excursion = (
                    edge
                    if z.excursion is None
                    else (min if side == 1 else max)(z.excursion, edge)
                )
                if z.tf in closed:
                    if z.retest:
                        z.expires = t - 1  # failed retest; NEVER flip indefinitely
                        notes.append(f"{z.key}:retest_invalidated")
                    else:
                        bos = z.key == self.frames[z.tf].latest_key[side]
                        if bos:
                            breaks.append((z.tf, -side))
                            self.frames[z.tf].trend = -side
                        z.retest = "bos_retest" if bos else "break_retest"
                        z.role, z.break_time, z.pierced, z.used, z.excursion = (
                            -side,
                            t,
                            False,
                            False,
                            None,
                        )
                        notes.append(
                            f"{z.key}:{'BOS' if bos else 'accepted_break'}_wait_retest"
                        )
                elif not z.used:
                    notes.append(f"{z.key}:outside_wait_source_close")
                continue
            if z.used or not old or not atr or old.t + 60 != b.t:
                continue
            extreme = b.l if side == 1 else b.h
            touched = b.l <= z.price + z.w and b.h >= z.price - z.w
            if not touched or (side * (old.c - z.price) < -z.w and not z.pierced):
                continue
            if d <= z.w:
                notes.append(f"{z.key}:inside_wait")
                continue
            swept = z.pierced or side * (extreme - z.price) < -z.w
            reason = z.retest or ("sweep_reclaim" if swept else "respected_pullback")
            invalid = (min if side == 1 else max)(
                extreme,
                z.price - side * z.w,
                z.excursion if z.excursion is not None else extreme,
            )
            z.pierced, z.excursion = False, None
            if side * mom < self.r.momentum_min or d - z.w > self.r.max_chase_atr * atr:
                notes.append(f"{z.key}:weak_or_extended")
                continue
            notes.append(f"{z.key}:{reason}")
            candidates.append((z, reason, invalid))
            z.used = True
        touched = {n.rsplit(":", 1)[0] for n in notes}
        observed = tuple(
            (z.key, z.price - z.w, z.price + z.w)
            for z in self.zones
            if z.key in touched
        )
        self.zones = [z for z in self.zones if t <= z.expires]
        trends = tuple(f.trend for f in self.frames.values())
        common = {
            "t": t,
            "atr": atr,
            "momentum": mom,
            "trends": trends,
            "breaks": tuple(breaks),
            "bar_direction": (1 if body > 0 else -1 if body < 0 else 0),
            "candle": self.last_patterns[1],
            "previous_candle": prev_pattern,
            "trigger_high": b.h,
            "trigger_low": b.l,
            "prior_momentum": self.last_momentum if old and old.t + 60 == b.t else 0.0,
            "reactions": tuple(notes),
            "observed_levels": observed,
        }
        if not self.frames[15].atr:
            answer = Decision(reason="warming_up", **common)
        elif candidates and (
            len({z.role for z, _, _ in candidates}) > 1
            or any(s == -candidates[0][0].role for _, s in breaks)
        ):
            answer = Decision(reason="conflicting_structure", **common)
        elif candidates:
            z, reason, invalid = max(
                candidates,
                key=lambda x: (
                    x[0].tf,
                    x[1] == "bos_retest",
                    x[0].touches,
                    -abs(b.c - x[0].price),
                ),
            )
            answer = Decision(
                reason=reason,
                side=z.role,
                tf=z.tf,
                zone_id=z.key,
                level=z.price,
                width=z.w,
                swing=z.label,
                touches=z.touches,
                invalidation=invalid,
                source_candle=self.last_patterns[z.tf],
                prior_at_zone=bool(
                    old
                    and old.t >= max(z.born, z.break_time)
                    and old.l <= z.price + z.w
                    and old.h >= z.price - z.w
                ),
                objective=self._objective(z.role, b.c),
                pool=(
                    "buy_side"
                    if (z.role if not z.retest else -z.role) == -1
                    else "sell_side"
                ),
                line_confluence=self.frames[z.tf].line_near(
                    z.role, b.t, z.price, z.w, self.r.recency_bars
                ),
                **common,
            )
        else:
            answer = Decision(
                reason=notes[-1].rsplit(":", 1)[-1] if notes else "away_from_zone",
                **common,
            )
        # Confirm pivots only AFTER evaluating this bar against previously known levels.
        for f in completed:
            for role, price, label in f.confirmed():
                self._add(f, role, price, label, t)
        self.last_momentum = mom
        return answer
