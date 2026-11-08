"""Synthetic forecasts and measurements for the core tests."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pvfc_core.archive import MeasuredBin, Snapshot
from pvfc_core.forecast_format import Forecast, ForecastPoint
from pvfc_core.timeline import BIN, BIN_SECONDS, INTERVAL, day_bounds, floor_to, steps

TZ = ZoneInfo("Europe/Berlin")


def curve(
    day: date,
    *,
    peak: float = 4.0,
    first_hour: float = 7.0,
    last_hour: float = 17.0,
    bands: bool = True,
    p10_share: float = 0.6,
    p90_share: float = 1.3,
    tz: ZoneInfo = TZ,
) -> list[ForecastPoint]:
    """Bell-shaped day: 30-minute intervals, zero outside ``first_hour`` .. ``last_hour``."""
    start, end = day_bounds(day, tz)
    points = []
    for interval in steps(start, end, INTERVAL):
        local = interval.astimezone(tz)
        middle = local.hour + local.minute / 60 + 0.25
        value = 0.0
        if first_hour < middle < last_hour:
            value = peak * math.sin(math.pi * (middle - first_hour) / (last_hour - first_hour))
        points.append(
            ForecastPoint(
                interval,
                value,
                value * p10_share if bands else None,
                value * p90_share if bands else None,
            )
        )
    return points


def forecast(days: list[date], **kwargs) -> Forecast:
    points: list[ForecastPoint] = []
    for day in days:
        points.extend(curve(day, **kwargs))
    return Forecast.from_points(points)


def bins_from(
    points: list[ForecastPoint],
    ratio=1.0,
    *,
    curtailed: set[datetime] | None = None,
) -> list[MeasuredBin]:
    """Measured bins that follow the forecast times ``ratio`` (number or f(start))."""
    result = []
    for point in points:
        factor = ratio(point.start) if callable(ratio) else ratio
        for start in steps(point.start, point.start + INTERVAL, BIN):
            result.append(
                MeasuredBin(
                    start,
                    point.p50 * factor * BIN_SECONDS / 3600,
                    BIN_SECONDS,
                    bool(curtailed and floor_to(start, INTERVAL) in curtailed),
                )
            )
    return result


def daily_snapshots(days: list[date], **kwargs) -> list[Snapshot]:
    """One snapshot per day at local midnight holding that day and the next."""
    snapshots = []
    for day in days:
        start, _ = day_bounds(day, TZ)
        snapshots.append(
            Snapshot(start, forecast([day, day + timedelta(days=1)], **kwargs), refreshed=True)
        )
    return snapshots


def day_range(first: date, count: int) -> list[date]:
    return [first + timedelta(days=offset) for offset in range(count)]
