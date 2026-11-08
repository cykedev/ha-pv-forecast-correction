"""Read and write the forecast attribute format of the Solcast integration.

A forecast is a list of periods with ``period_start`` (timezone-aware datetime or ISO
string) and the mean power over the period in kW: ``pv_estimate`` (median, P50) and
optionally ``pv_estimate10`` / ``pv_estimate90`` (P10 / P90). Lists come in 30-minute
(``detailedForecast``) or 60-minute (``detailedHourly``) resolution. Internally every
forecast is held in 30-minute intervals.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from itertools import pairwise
from typing import Any

from .timeline import INTERVAL, INTERVAL_HOURS, as_utc, hour_key

ATTR_DETAILED_FORECAST = "detailedForecast"
ATTR_DETAILED_HOURLY = "detailedHourly"
KEY_PERIOD_START = "period_start"
KEY_P50 = "pv_estimate"
KEY_P10 = "pv_estimate10"
KEY_P90 = "pv_estimate90"

_SUPPORTED_PERIODS = (timedelta(minutes=30), timedelta(minutes=60))
_DECIMALS = 4


class ForecastFormatError(ValueError):
    """The attributes do not contain a usable forecast list."""


@dataclass(frozen=True, slots=True)
class ForecastPoint:
    """Mean power in kW over one 30-minute interval starting at ``start`` (UTC)."""

    start: datetime
    p50: float
    p10: float | None = None
    p90: float | None = None

    def scaled(self, factor: float) -> ForecastPoint:
        """The same interval with every value multiplied by ``factor``."""
        return ForecastPoint(
            self.start,
            self.p50 * factor,
            None if self.p10 is None else self.p10 * factor,
            None if self.p90 is None else self.p90 * factor,
        )


@dataclass(frozen=True, slots=True)
class Forecast:
    """An ordered set of 30-minute forecast intervals."""

    points: tuple[ForecastPoint, ...] = ()

    @classmethod
    def from_points(cls, points: Iterable[ForecastPoint]) -> Forecast:
        """Build a forecast; on duplicate starts the first point wins."""
        unique: dict[datetime, ForecastPoint] = {}
        for point in points:
            unique.setdefault(as_utc(point.start), point)
        return cls(tuple(unique[start] for start in sorted(unique)))

    def __bool__(self) -> bool:
        return bool(self.points)

    @property
    def has_bands(self) -> bool:
        """True when every interval carries P10 and P90."""
        return bool(self.points) and all(
            p.p10 is not None and p.p90 is not None for p in self.points
        )

    def as_dict(self) -> dict[datetime, ForecastPoint]:
        """Intervals keyed by their start."""
        return {p.start: p for p in self.points}

    def between(self, start: datetime, end: datetime) -> list[ForecastPoint]:
        """Intervals that start in ``[start, end)``."""
        return [p for p in self.points if start <= p.start < end]

    def merged_with(self, other: Forecast) -> Forecast:
        """Union of two forecasts; intervals of ``self`` take precedence."""
        return Forecast.from_points((*self.points, *other.points))


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as err:
            raise ForecastFormatError(f"invalid period_start: {value!r}") from err
    else:
        raise ForecastFormatError(f"invalid period_start: {value!r}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForecastFormatError(f"period_start without time zone: {value!r}")
    return as_utc(parsed)


def _parse_list(items: Sequence[Any]) -> list[ForecastPoint]:
    points: list[ForecastPoint] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ForecastFormatError(f"forecast entry is not a mapping: {item!r}")
        if KEY_PERIOD_START not in item:
            raise ForecastFormatError("forecast entry without period_start")
        p50 = _to_float(item.get(KEY_P50))
        if p50 is None:
            raise ForecastFormatError(f"forecast entry without numeric {KEY_P50}")
        p10 = _to_float(item.get(KEY_P10))
        p90 = _to_float(item.get(KEY_P90))
        points.append(
            ForecastPoint(
                start=_to_datetime(item[KEY_PERIOD_START]),
                p50=max(p50, 0.0),
                p10=None if p10 is None else max(p10, 0.0),
                p90=None if p90 is None else max(p90, 0.0),
            )
        )
    points.sort(key=lambda p: p.start)
    return points


def _period(points: Sequence[ForecastPoint], default: timedelta) -> timedelta:
    gaps = {b.start - a.start for a, b in pairwise(points)}
    gaps.discard(timedelta(0))
    if not gaps:
        return default
    period = min(gaps)
    if period not in _SUPPORTED_PERIODS:
        raise ForecastFormatError(f"unsupported forecast resolution: {period}")
    return period


def parse_forecast(attributes: Mapping[str, Any]) -> Forecast:
    """Parse the forecast list of an entity's attributes.

    ``detailedForecast`` (30 min) is preferred; ``detailedHourly`` (60 min) is used when
    the 30-minute list is missing or empty. Hourly periods are split into two 30-minute
    intervals with the same mean power.

    Raises ForecastFormatError when no usable list is present.
    """
    for key, default in (
        (ATTR_DETAILED_FORECAST, timedelta(minutes=30)),
        (ATTR_DETAILED_HOURLY, timedelta(minutes=60)),
    ):
        items = attributes.get(key)
        if isinstance(items, Sequence) and not isinstance(items, str) and items:
            return _from_items(items, default)
    raise ForecastFormatError(f"no {ATTR_DETAILED_FORECAST} or {ATTR_DETAILED_HOURLY} attribute")


def _from_items(items: Sequence[Any], default: timedelta) -> Forecast:
    points = _parse_list(items)
    period = _period(points, default)
    if period == INTERVAL:
        return Forecast.from_points(points)
    halves: list[ForecastPoint] = []
    for point in points:
        halves.append(point)
        halves.append(ForecastPoint(point.start + INTERVAL, point.p50, point.p10, point.p90))
    return Forecast.from_points(halves)


def energy(points: Iterable[ForecastPoint], band: str = "p50") -> float | None:
    """Energy in kWh of the given band; None if any interval lacks the band."""
    total = 0.0
    for point in points:
        value = getattr(point, band)
        if value is None:
            return None
        total += value * INTERVAL_HOURS
    return total


def _rounded(value: float) -> float:
    return round(value, _DECIMALS) + 0.0  # + 0.0 turns -0.0 into 0.0


def _entry(start: datetime, p50: float, p10: float | None, p90: float | None, tz: tzinfo):
    entry: dict[str, Any] = {
        KEY_PERIOD_START: start.astimezone(tz),
        KEY_P50: _rounded(p50),
    }
    if p10 is not None and p90 is not None:
        # Some consumers fall back to P90 when P50 is 0: keep the band closed at night.
        entry[KEY_P10] = 0.0 if p50 <= 0 else _rounded(p10)
        entry[KEY_P90] = 0.0 if p50 <= 0 else _rounded(p90)
    return entry


def detailed_forecast(points: Sequence[ForecastPoint], tz: tzinfo) -> list[dict[str, Any]]:
    """30-minute list in the Solcast format, ``period_start`` in local time."""
    bands = all(p.p10 is not None and p.p90 is not None for p in points)
    return [
        _entry(p.start, p.p50, p.p10 if bands else None, p.p90 if bands else None, tz)
        for p in points
    ]


def detailed_hourly(points: Sequence[ForecastPoint], tz: tzinfo) -> list[dict[str, Any]]:
    """60-minute list (mean of the two halves of each local clock hour).

    A half that is missing counts as zero power, so hours at the edges of a partial list
    keep the correct energy.
    """
    bands = all(p.p10 is not None and p.p90 is not None for p in points)
    hours: dict[datetime, list[ForecastPoint]] = {}
    for point in points:
        hours.setdefault(hour_key(point.start, tz), []).append(point)
    ratio = INTERVAL / timedelta(hours=1)
    result = []
    for start in sorted(hours):
        group = hours[start]
        p50 = sum(p.p50 for p in group) * ratio
        p10 = sum(p.p10 or 0.0 for p in group) * ratio if bands else None
        p90 = sum(p.p90 or 0.0 for p in group) * ratio if bands else None
        result.append(_entry(start, p50, p10, p90, tz))
    return result


def rounded_energy(value: float | None) -> float | None:
    """Energy rounded for publication."""
    return None if value is None else _rounded(value)
