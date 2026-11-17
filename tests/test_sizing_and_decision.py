from bot.decision.signal_fusion import SignalContext, decide
from bot.risk.sizing import SizingRules, size_from_risk


def test_risk_sizing_respects_total_exposure_and_step() -> None:
    result = size_from_risk(
        equity=10_000,
        risk_pct=0.5,
        stop_distance=2,
        value_per_price_unit_per_lot=100,
        open_lots=0.2,
        rules=SizingRules(0.01, 0.5, 0.01, 0.5),
    )
    assert result == 0.25


def test_sizing_returns_zero_when_remaining_exposure_is_too_small() -> None:
    result = size_from_risk(
        equity=10_000,
        risk_pct=0.5,
        stop_distance=2,
        value_per_price_unit_per_lot=100,
        open_lots=0.495,
        rules=SizingRules(0.01, 0.5, 0.01, 0.5),
    )
    assert result == 0.0


def test_signal_gate_uses_probability_and_hard_filters() -> None:
    context = SignalContext(0.7, 0.6, True, True, 0.2, 0.1)
    assert decide(context).allowed
    assert decide(SignalContext(0.9, 0.6, False, True, 0.2, 0.1)).reason == "spread_filter"
