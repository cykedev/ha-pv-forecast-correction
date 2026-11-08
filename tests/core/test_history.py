"""Day sums from hourly history."""

from datetime import date, timedelta

import pytest

from pvfc_core.history import HistorySkip, days_from_hourly
from pvfc_core.timeline import day_bounds

from .synthetic import TZ

DAY = date(2026, 11, 2)


def hour(day: date, local_hour: int):
    start, _ = day_bounds(day, TZ)
    return start + timedelta(hours=local_hour)


def series(day: date, produce=range(8, 17), kwh=1.0):
    return {hour(day, h): (kwh if h in produce else 0.0) for h in range(24)}


def test_uses_the_forecast_from_before_production():
    energy = series(DAY)
    forecast = {hour(DAY, h): 20.0 + h for h in range(24)}  # revised during the day
    [result] = days_from_hourly(energy, forecast, TZ, DAY, DAY)
    assert result.usable
    assert result.actual_kwh == pytest.approx(9.0)
    assert result.forecast_kwh == 27.0  # total at the end of 07:00-08:00
    assert result.forecast_as_of == hour(DAY, 8)


def test_looks_back_a_few_hours_for_the_forecast():
    forecast = {hour(DAY, 5): 15.0}
    [result] = days_from_hourly(series(DAY), forecast, TZ, DAY, DAY)
    assert result.forecast_kwh == 15.0
    [none] = days_from_hourly(series(DAY), {hour(DAY, 3): 15.0}, TZ, DAY, DAY)
    assert none.skipped is HistorySkip.NO_FORECAST


def test_skips_days_with_gaps_or_without_production():
    energy = series(DAY)
    del energy[hour(DAY, 12)]
    forecast = {hour(DAY, h): 20.0 for h in range(24)}
    [gap] = days_from_hourly(energy, forecast, TZ, DAY, DAY)
    assert gap.skipped is HistorySkip.MISSING_HOURS
    night = {hour(DAY, h): 0.0 for h in range(24)}
    [dark] = days_from_hourly(night, forecast, TZ, DAY, DAY)
    assert dark.skipped is HistorySkip.NO_PRODUCTION


def test_daylight_saving_day_has_25_hours():
    day = date(2026, 10, 25)
    energy = {hour(day, h): (1.0 if 8 <= h < 17 else 0.0) for h in range(25)}
    forecast = {hour(day, h): 18.0 for h in range(25)}
    [result] = days_from_hourly(energy, forecast, TZ, day, day)
    assert result.actual_kwh == pytest.approx(9.0)
    assert result.usable


def test_several_days():
    energy = series(DAY) | series(DAY + timedelta(days=1), kwh=2.0)
    forecast = dict.fromkeys(energy, 30.0)
    results = days_from_hourly(energy, forecast, TZ, DAY, DAY + timedelta(days=2))
    assert [r.usable for r in results] == [True, True, False]
    assert results[1].actual_kwh == pytest.approx(18.0)


def test_a_counter_blip_at_night_is_not_the_start_of_production():
    energy = series(DAY)
    energy[hour(DAY, 1)] = 0.002
    forecast = {hour(DAY, h): 20.0 + h for h in range(24)}
    [result] = days_from_hourly(energy, forecast, TZ, DAY, DAY)
    assert result.forecast_as_of == hour(DAY, 8)
    assert result.actual_kwh == pytest.approx(9.002)


def test_forecast_per_hour_gives_day_sum_and_hours():
    energy = series(DAY)
    forecast = {hour(DAY, h): 40.0 for h in range(24)}  # day total far off: not used
    hourly = {hour(DAY, h): (2.0 if 8 <= h < 17 else 0.0) for h in range(24)}
    [result] = days_from_hourly(energy, forecast, TZ, DAY, DAY, hourly_forecast=hourly)
    assert result.forecast_kwh == pytest.approx(18.0)
    assert result.actual_kwh == pytest.approx(9.0)
    assert len(result.hours) == 9
    assert result.hours[0] == (8, 1.0, 2.0)


def test_falls_back_to_the_day_total_without_hourly_forecast():
    energy = series(DAY)
    forecast = {hour(DAY, h): 20.0 for h in range(24)}
    hourly = {hour(DAY, h): 2.0 for h in range(8, 12)}  # incomplete: not used
    [result] = days_from_hourly(energy, forecast, TZ, DAY, DAY, hourly_forecast=hourly)
    assert result.forecast_kwh == 20.0
    assert result.hours == ()
