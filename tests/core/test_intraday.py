"""Layer B, short-term part: the multiplier, the ratio gate and the nightly choice."""

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest

from pvfc_core.fog import FogGate, FogSettings
from pvfc_core.intraday import (
    FogPart,
    IntradaySettings,
    Tuning,
    TuningDay,
    day_loss,
    fog_allowed,
    multiplier,
    scoring_hours,
    tune,
)

GATE_07 = FogGate(threshold=0.7, min_confidence=None)
FOG = FogSettings(gate=GATE_07)
SETTINGS = IntradaySettings()
FIRST = date(2026, 11, 1)


def test_the_last_hour_fades_into_the_fog_factor():
    assert multiplier(0.5, 1.0, 0.0, 1.0) == pytest.approx(0.5)
    assert multiplier(0.5, 1.0, 1.0, 1.0) == pytest.approx(math.exp(math.exp(-1) * math.log(0.5)))
    assert multiplier(0.5, 1.0, 12.0, 1.0) == pytest.approx(1.0, abs=1e-4)
    assert multiplier(0.5, 0.4, 12.0, 1.0) == pytest.approx(0.4, abs=1e-4)
    # Without a ratio for the last hour the fog factor alone applies.
    assert multiplier(None, 0.4, 0.0, 1.0) == 0.4
    assert multiplier(None, 1.0, 0.0, 1.0) == 1.0


def test_the_ratio_needs_expected_energy_and_a_measurement():
    assert SETTINGS.ratio(0.5, 1.0, 10.0) == pytest.approx(0.5)
    assert SETTINGS.ratio(0.5, 0.2, 10.0) is None  # less than 3 % of the day expected
    assert SETTINGS.ratio(0.0, 1.0, 10.0) is None  # nothing measured: snow, outage, stuck meter
    assert SETTINGS.ratio(0.05, 1.0, 10.0) == SETTINGS.min_ratio
    assert SETTINGS.ratio(5.0, 1.0, 10.0) == SETTINGS.max_ratio


def bell(peak: float = 2.0, first: int = 8, last: int = 16) -> dict[int, float]:
    """Expected kWh per local hour of a winter day."""
    return {
        h: peak * math.sin(math.pi * (h + 0.5 - first) / (last - first)) for h in range(first, last)
    }


def day(index: int, ratio, *, asof: bool = True) -> TuningDay:
    """A day measured at ``ratio(hour)`` of the expected energy; the base never changes."""
    expected = bell()
    actual = {h: e * ratio(h) for h, e in expected.items()}
    forecasts = (
        {h: {j: e for j, e in expected.items() if j >= h} for h in expected} if asof else None
    )
    return TuningDay(FIRST + timedelta(days=index), expected, actual, forecasts)


def test_scoring_hours_cover_daylight_only():
    assert scoring_hours(day(0, lambda h: 1.0)) == list(range(9, 16))


def test_persistent_fog_is_handed_to_the_fog_layer():
    days = [day(i, lambda h: 0.4) for i in range(8)]
    tuning = tune(days, FOG, SETTINGS)
    assert tuning.use_fog
    assert tuning.loss_fog < (1 - SETTINGS.margin) * tuning.loss_follow
    assert tuning.days == tuple(d.day for d in days)
    assert tuning.with_asof == 8


def test_mornings_that_clear_up_keep_the_fog_layer_out():
    days = [day(i, lambda h: 0.4 if h < 12 else 1.0) for i in range(8)]
    assert not tune(days, FOG, SETTINGS).use_fog


def test_too_few_days_keep_the_fog_layer_out():
    days = [day(i, lambda h: 0.4) for i in range(SETTINGS.min_days - 1)]
    tuning = tune(days, FOG, SETTINGS)
    assert not tuning.use_fog
    assert tuning.days == tuple(d.day for d in days)  # progress is visible
    assert tuning.loss_fog is None


def test_without_the_short_term_part_the_fog_layer_is_scored_alone():
    # Dull mornings that brighten at noon: with the short-term part the fog layer is not
    # needed, but without it the fog layer is the only correction there is.
    days = [day(i, lambda h: 0.4 if h < 12 else 0.75) for i in range(8)]
    assert not tune(days, FOG, SETTINGS).use_fog
    assert tune(days, FOG, IntradaySettings(enabled=False)).use_fog
    plain = day(0, lambda h: 0.5)
    assert day_loss(plain, False, FOG, IntradaySettings(enabled=False)) == pytest.approx(
        sum(day_loss_plain(plain))
    )


def day_loss_plain(record: TuningDay) -> list[float]:
    """Errors of the forecast with no layer B at all, as day_loss sums them."""
    errors = []
    hours = scoring_hours(record)
    for index, hour in enumerate(hours):
        if index > 0:
            rest = sum(v for h, v in record.actual.items() if h >= hour)
            errors.append(abs(sum(v for h, v in record.expected.items() if h >= hour) - rest))
        soon = range(hour, hour + 3)
        errors.append(
            abs(
                sum(record.expected.get(h, 0.0) for h in soon)
                - sum(record.actual.get(h, 0.0) for h in soon)
            )
        )
    return errors


@pytest.mark.parametrize("recent_hours", [0.5, 1.0, 1.5, 2.0, 6.0])
def test_every_recent_window_lets_the_fog_layer_be_chosen(recent_hours):
    fog = FogSettings(gate=GATE_07, recent_hours=recent_hours)
    days = [day(i, lambda h: 0.4) for i in range(8)]
    assert tune(days, fog, SETTINGS).use_fog


def test_the_confidence_condition_is_scored_like_it_runs():
    confident_gate = FogSettings(gate=FogGate(threshold=0.7, min_confidence=0.5))
    unsure = [replace(day(i, lambda h: 0.4), confidence=0.3) for i in range(8)]
    sure = [replace(day(i, lambda h: 0.4), confidence=0.8) for i in range(8)]
    unknown = [day(i, lambda h: 0.4) for i in range(8)]
    assert not tune(unsure, confident_gate, SETTINGS).use_fog
    assert tune(sure, confident_gate, SETTINGS).use_fog
    assert tune(unknown, confident_gate, SETTINGS).use_fog  # no band known: no condition


def test_only_the_most_recent_days_count():
    old_fog = [day(i, lambda h: 0.4) for i in range(10)]
    recent_clear = [day(10 + i, lambda h: 0.4 if h < 12 else 1.0) for i in range(14)]
    assert not tune(old_fog + recent_clear, FOG, SETTINGS).use_fog


def test_days_without_forecasts_as_of_each_hour_use_their_record():
    fog_days = [day(i, lambda h: 0.4, asof=False) for i in range(8)]
    tuning = tune(fog_days, FOG, SETTINGS)
    assert tuning.use_fog
    assert tuning.with_asof == 0


def test_a_perfect_forecast_costs_nothing():
    perfect = day(0, lambda h: 1.0)
    assert day_loss(perfect, False, FOG, SETTINGS) == pytest.approx(0.0, abs=1e-9)
    assert day_loss(perfect, True, FOG, SETTINGS) == pytest.approx(0.0, abs=1e-9)


def test_the_setting_overrides_the_nightly_choice():
    chose_fog = Tuning(use_fog=True)
    chose_short_term = Tuning(use_fog=False)
    assert fog_allowed(IntradaySettings(fog_part=FogPart.AUTO), chose_fog)
    assert not fog_allowed(IntradaySettings(fog_part=FogPart.AUTO), chose_short_term)
    assert fog_allowed(IntradaySettings(fog_part=FogPart.ALWAYS), chose_short_term)
    assert not fog_allowed(IntradaySettings(fog_part=FogPart.NEVER), chose_fog)


def test_records_and_tuning_survive_a_round_trip():
    record = replace(day(0, lambda h: 0.5), confidence=0.42)
    assert TuningDay.from_dict(record.to_dict()) == TuningDay(
        record.day,
        {h: round(v, 4) for h, v in record.expected.items()},
        {h: round(v, 4) for h, v in record.actual.items()},
        {h: {j: round(v, 4) for j, v in f.items()} for h, f in record.asof.items()},
        0.42,
    )
    tuning = tune([day(i, lambda h: 0.4) for i in range(8)], FOG, SETTINGS)
    restored = Tuning.from_dict(tuning.to_dict())
    assert restored.use_fog == tuning.use_fog
    assert restored.days == tuning.days
    assert restored.loss_fog == pytest.approx(tuning.loss_fog, abs=1e-3)
