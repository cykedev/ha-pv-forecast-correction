"""Day sums for the level factor from hourly history (e.g. long-term statistics).

The level factor learns from the forecast of record: the value the base forecast held for
each interval when it began. Recorders do not keep the forecast lists, but they keep the
base forecast's day total and the measured energy per hour. The day total as it stood
before the first hour with production is used in place of the forecast of record; on the
reference data it matched the exact value without bias (8 % per day, which averages out
over the days the factor uses). The day total at the end of the day does not work: a
forecast provider may revise intervals that have passed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from enum import StrEnum

from .timeline import as_utc, day_bounds, steps

HOUR = timedelta(hours=1)
MIN_HOURLY_KWH = 0.001
"""An hour with at least this much energy counts as producing ..."""
MIN_PEAK_SHARE = 0.01
"""... and at least this share of the day's strongest hour (ignores counter blips at night)."""
FORECAST_LOOKBACK_HOURS = 3
"""How far before the first producing hour a missing forecast value may be looked up."""


class HistorySkip(StrEnum):
    """Why a day could not be used."""

    NO_PRODUCTION = "no_production"
    MISSING_HOURS = "missing_hours"
    NO_FORECAST = "no_forecast"


@dataclass(frozen=True, slots=True)
class HistoryDay:
    """Sums of one past day, or the reason it was skipped."""

    day: date
    actual_kwh: float | None = None
    forecast_kwh: float | None = None
    forecast_as_of: datetime | None = None
    skipped: HistorySkip | None = None

    @property
    def usable(self) -> bool:
        return self.skipped is None


def days_from_hourly(
    energy: Mapping[datetime, float],
    forecast: Mapping[datetime, float],
    tz: tzinfo,
    first_day: date,
    last_day: date,
) -> list[HistoryDay]:
    """Day sums from hourly series keyed by the hour's start (UTC).

    ``energy`` is the energy measured in each hour (kWh). ``forecast`` is the base
    forecast's total for the day as it stood at the end of each hour (kWh). A day is used
    when every hour between its first and last producing hour has a measurement and a
    forecast total is known before production began.
    """
    energy = {as_utc(k): v for k, v in energy.items()}
    forecast = {as_utc(k): v for k, v in forecast.items()}
    result: list[HistoryDay] = []
    day = first_day
    while day <= last_day:
        result.append(_day(day, energy, forecast, tz))
        day += timedelta(days=1)
    return result


def _day(
    day: date, energy: Mapping[datetime, float], forecast: Mapping[datetime, float], tz: tzinfo
) -> HistoryDay:
    start, end = day_bounds(day, tz)
    hours = list(steps(start, end, HOUR))
    peak = max((energy.get(hour, 0.0) for hour in hours), default=0.0)
    threshold = max(MIN_HOURLY_KWH, MIN_PEAK_SHARE * peak)
    producing = [hour for hour in hours if energy.get(hour, 0.0) >= threshold]
    if not producing:
        return HistoryDay(day, skipped=HistorySkip.NO_PRODUCTION)
    first, last = producing[0], producing[-1]
    if any(hour not in energy for hour in steps(first, last + HOUR, HOUR)):
        return HistoryDay(day, skipped=HistorySkip.MISSING_HOURS)
    actual = sum(max(energy.get(hour, 0.0), 0.0) for hour in hours)

    total = as_of = None
    for back in range(1, FORECAST_LOOKBACK_HOURS + 1):
        hour = first - back * HOUR
        if hour < start:
            break
        if forecast.get(hour) is not None:
            total, as_of = forecast[hour], hour + HOUR
            break
    if total is None and forecast.get(first) is not None:
        total, as_of = forecast[first], first + HOUR  # production right after midnight
    if total is None or total <= 0:
        return HistoryDay(day, actual_kwh=actual, skipped=HistorySkip.NO_FORECAST)
    return HistoryDay(day, actual_kwh=actual, forecast_kwh=total, forecast_as_of=as_of)
