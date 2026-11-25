"""Clock contract for a new Hull entry. Candle, broker tick and check are distinct."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from .orders import Block

QUOTE_POLICY = "first_fresh_post_close_tick_v1"
WAITING = "waiting_first_post_close_tick"
FAST_POLL_S = 0.25


def utc(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds")


def _number(v):
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return x if math.isfinite(x) else None


@dataclass(frozen=True, slots=True)
class QuoteCheck:
    signal: float
    checked: float
    tick_msc: float | None
    bid: float | None
    ask: float | None
    entry_limit: float
    tick_limit: float
    tick_status: str
    reason: str

    @property
    def waitable(self):
        return self.checked < self.signal + self.entry_limit and self.reason in (
            "quote_missing",
            "quote_precedes_signal_close",
            "quote_too_old",
        )

    def audit(self, tf):
        try:
            tick_utc = utc(self.tick_msc / 1000) if self.tick_msc is not None else None
        except (OverflowError, OSError, ValueError):
            tick_utc = (
                None  # retain the raw number even for a nonsensical broker timestamp
            )
        end = self.signal
        return {
            "entry_timing": "confirmed_close",
            "trigger_utc": utc(self.signal),
            "trigger_msc": round(self.signal * 1000),
            "bar_open_utc": utc(end - tf * 60),
            "bar_end_utc": utc(end),
            "signal_close_utc": utc(self.signal),
            "checked_at_utc": utc(self.checked),
            "checked_unix_s": self.checked,
            "deadline_utc": utc(self.signal + self.entry_limit),
            "tick_time_msc": self.tick_msc,
            "tick_utc": tick_utc,
            "bid": self.bid,
            "ask": self.ask,
            "signal_age_ms": round((self.checked - self.signal) * 1000, 3),
            "quote_age_ms": (
                round(self.checked * 1000 - self.tick_msc, 3)
                if self.tick_msc is not None
                else None
            ),
            "max_entry_age_s": self.entry_limit,
            "max_quote_age_s": self.tick_limit,
            "tick_status": self.tick_status,
            "reason": self.reason,
        }


CLOCK_SKEW_TOLERANCE_S = 2.0


def check_quote(
    signal,
    now,
    limits,
    tick_msc,
    bid,
    ask,
    *,
    monotonic_expired=False,
):
    """Only confirmed labels may wait for a missing, pre-close or aged-out first tick.

    Broken prices and a broker clock materially in the future (> CLOCK_SKEW_TOLERANCE_S)
    are hard failures. Small sub-2s skew (e.g. Windows NTP drift) is tolerated.
    The rate/tick timestamps are UTC epochs; never reinterpret broker time here.
    """
    missing = tick_msc is None and bid is None and ask is None
    ms, b, a = _number(tick_msc), _number(bid), _number(ask)
    if missing:
        status = "quote_missing"
    elif ms is None or ms <= 0 or b is None or a is None or not 0 < b <= a:
        status = "invalid_quote"
    elif ms / 1000 > now + CLOCK_SKEW_TOLERANCE_S:
        status = "quote_clock_ahead"
    elif ms / 1000 < signal:
        status = "quote_precedes_signal_close"
    elif now - ms / 1000 > limits.max_quote_age_s:
        status = "quote_too_old"
    else:
        status = "ready"
    reason = (
        "signal_clock_ahead"
        if now < signal
        else "entry_window_expired"
        if now > signal + limits.max_entry_age_s or monotonic_expired
        else status
    )
    return QuoteCheck(
        signal,
        now,
        ms,
        b,
        a,
        limits.max_entry_age_s,
        limits.max_quote_age_s,
        status,
        reason,
    )


class TimingBlock(Block):
    def __init__(self, check, stage):
        super().__init__(check.reason)
        self.check, self.stage = check, stage


def poll_delay(now, frames, normal, entry_age, waiting):
    """Wake at each selected native close; check promptly while MT5 rolls its bar.

    Sleeping 10s just before an M1 boundary can otherwise miss an 8s signal
    window entirely. No thread or per-symbol wait can hold up another lane.
    """
    phases = [(tf * 60, now % (tf * 60)) for tf in frames]
    delay = min(normal, *(period - phase for period, phase in phases))
    if waiting or any(phase <= entry_age for _, phase in phases):
        delay = min(delay, FAST_POLL_S)
    return delay
