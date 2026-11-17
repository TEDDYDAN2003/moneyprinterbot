from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SignalContext:
    entry_probability: float
    minimum_probability: float
    spread_ok: bool
    risk_allowed: bool
    expected_value: float
    minimum_expected_value: float


@dataclass(frozen=True)
class SignalDecision:
    allowed: bool
    reason: str


def decide(context: SignalContext) -> SignalDecision:
    if not 0 <= context.entry_probability <= 1:
        raise ValueError("entry_probability must be between zero and one")
    if not context.spread_ok:
        return SignalDecision(False, "spread_filter")
    if not context.risk_allowed:
        return SignalDecision(False, "risk_governor")
    if context.entry_probability < context.minimum_probability:
        return SignalDecision(False, "entry_probability")
    if context.expected_value < context.minimum_expected_value:
        return SignalDecision(False, "expected_value")
    return SignalDecision(True, "approved")
