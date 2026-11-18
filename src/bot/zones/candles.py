"""Descriptive price action on finished candles. No order-book or prediction claims."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import Bar


def patterns(bar: Bar, prev: Bar | None, tick: float) -> tuple[str, ...]:
    span = bar.h - bar.l
    if span < tick / 2:
        return ("flat",)
    body = abs(bar.c - bar.o)
    up = bar.h - max(bar.o, bar.c)
    down = min(bar.o, bar.c) - bar.l
    sign = "bull" if bar.c > bar.o else "bear"
    tags = []
    if body <= 0.10 * span:
        tags.append("doji")
    if prev is not None:
        if bar.h <= prev.h and bar.l >= prev.l:
            tags.append("inside")
        # Opposite real bodies; cover both ends, extend at least one end.
        # A tiny doji engulfing another tiny body is not a directional trigger.
        if body >= 0.35 * span and abs(prev.c - prev.o) >= tick / 2:
            if (
                prev.c < prev.o
                and bar.c > bar.o
                and bar.o <= prev.c
                and bar.c >= prev.o
                and (bar.o < prev.c or bar.c > prev.o)
            ):
                tags.append("bull_engulf")
            if (
                prev.c > prev.o
                and bar.c < bar.o
                and bar.o >= prev.c
                and bar.c <= prev.o
                and (bar.o > prev.c or bar.c < prev.o)
            ):
                tags.append("bear_engulf")
    if body > 0.10 * span:
        if (
            down >= 2 * body
            and down >= 0.50 * span
            and (bar.c - bar.l) >= 0.67 * span
            and up <= 0.25 * span
        ):
            tags.append("bull_rejection")
        if (
            up >= 2 * body
            and up >= 0.50 * span
            and (bar.h - bar.c) >= 0.67 * span
            and down <= 0.25 * span
        ):
            tags.append("bear_rejection")
        if body >= 0.65 * span:
            tags.append(sign + "_body")
    return tuple(tags) or (sign + "_ordinary",)


def directional(tags, side):
    prefix = "bull_" if side == 1 else "bear_"
    return any(prefix + k in tags for k in ("engulf", "rejection"))
