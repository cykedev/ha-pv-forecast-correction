"""Layer B: the fog factor and its gates."""

from datetime import UTC, datetime

import pytest

from pvfc_core.fog import FogGate, FogInputs, FogReason, FogSettings, FogState, evaluate

NOW = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)


def inputs(morning=0.3, recent=0.3, expected=2.0, day=20.0, confidence=0.7) -> FogInputs:
    return FogInputs(
        expected_morning=expected,
        actual_morning=expected * morning,
        expected_recent=expected / 2,
        actual_recent=expected / 2 * recent,
        expected_day=day,
        confidence=confidence,
    )


def test_waits_for_enough_of_the_morning():
    state = evaluate(FogState(), inputs(expected=0.5, day=20.0), FogSettings(), NOW)
    assert state.reason is FogReason.WAITING
    assert state.k == 1.0


def test_no_forecast_today():
    state = evaluate(FogState(), inputs(expected=0.0, day=0.0), FogSettings(), NOW)
    assert state.reason is FogReason.NO_FORECAST


def test_consistency_gate_needs_morning_and_recent_below_threshold():
    settings = FogSettings(gate=FogGate(threshold=0.7, min_confidence=None))
    closed = evaluate(FogState(), inputs(morning=0.5, recent=0.9), settings, NOW)
    assert closed.reason is FogReason.GATE_CLOSED
    assert closed.k == 1.0
    opened = evaluate(FogState(), inputs(morning=0.5, recent=0.5), settings, NOW)
    assert opened.reason is FogReason.ACTIVE
    assert opened.k == pytest.approx(0.5)
    assert opened.active_since == NOW


def test_confident_miss_gate_needs_a_narrow_base():
    settings = FogSettings(gate=FogGate(threshold=0.7, min_confidence=0.5))
    uncertain = evaluate(FogState(), inputs(confidence=0.3), settings, NOW)
    assert uncertain.reason is FogReason.BASE_UNCERTAIN
    no_band = evaluate(FogState(), inputs(confidence=None), settings, NOW)
    assert no_band.reason is FogReason.NO_BAND
    assert evaluate(FogState(), inputs(confidence=0.6), settings, NOW).active


def test_active_layer_holds_for_the_day_and_never_raises():
    settings = FogSettings(gate=FogGate(threshold=0.7, min_confidence=None))
    state = evaluate(FogState(), inputs(morning=0.4, recent=0.4), settings, NOW)
    later = evaluate(state, inputs(morning=0.9, recent=1.5), settings, NOW)
    assert later.active
    assert later.k == pytest.approx(0.9)
    sunny = evaluate(later, inputs(morning=1.2, recent=1.5), settings, NOW)
    assert sunny.k == 1.0
    assert sunny.active_since == NOW


def test_factor_is_clamped_from_below():
    settings = FogSettings(gate=FogGate(threshold=None, min_confidence=None), min_factor=0.2)
    state = evaluate(FogState(), inputs(morning=0.05, recent=0.05), settings, NOW)
    assert state.k == 0.2


def test_ungated_opens_once_enough_was_measured():
    settings = FogSettings(gate=FogGate(threshold=None, min_confidence=None))
    state = evaluate(FogState(), inputs(morning=0.95, recent=1.2), settings, NOW)
    assert state.active
    assert state.k == pytest.approx(0.95)


def test_gate_names():
    assert FogGate(None, None).name == "ungated"
    assert FogGate(0.6, None).name == "consistency 0.6"
    assert FogGate(0.7, 0.5).name == "confident miss 0.7 / 0.5"


def test_state_round_trip():
    state = evaluate(FogState(), inputs(), FogSettings(), NOW)
    assert FogState.from_dict(state.to_dict()) == state
