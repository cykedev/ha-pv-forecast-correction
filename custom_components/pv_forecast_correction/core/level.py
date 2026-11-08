"""Layer A: level factor.

    f = (sum actual + K * mean_daily_forecast) / (sum forecast + K * mean_daily_forecast)

over the daylight intervals of the last N complete days. "forecast" is the forecast of
record: the value the base forecast held for an interval when the interval began.
Curtailed intervals and intervals without measurements are left out of both sums. The
K pseudo-days at ratio 1 shrink the factor toward 1.0, so a fresh install starts
neutral.

The factor can also vary over the day (time-of-day curve): the same sums per local clock
hour give a factor per hour, shrunk toward the day factor with W pseudo-days of an
average daylight hour,

    f_h = (actual_h + W * mean_hour * f) / (forecast_h + W * mean_hour)

so hours with little energy (dawn, dusk) stay close to the day factor, while hours that
run systematically below or above the rest of the day (morning fog in winter, shade at
low sun that the base does not know) get their own value.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .actuals import IntervalActual
from .timeline import INTERVAL_HOURS

MAX_MISSING_SHARE = 0.1
"""A day is complete when at most this share of its daylight intervals lacks data."""


@dataclass(frozen=True, slots=True)
class LevelSettings:
    """Parameters of layer A."""

    days: int = 14
    shrink_days: float = 2.0
    min_factor: float = 0.3
    max_factor: float = 1.5
    by_time: bool = True
    """Learn a factor per local clock hour (time-of-day curve)."""
    time_shrink_days: float = 2.0

    @property
    def max_age_days(self) -> int:
        """Days older than this are not used, even if fewer than ``days`` remain."""
        return 2 * self.days


@dataclass(frozen=True, slots=True)
class DayStats:
    """Forecast of record and measured energy of one local day."""

    day: date
    actual_kwh: float
    forecast_kwh: float
    intervals: int
    curtailed: int
    missing: int
    imported: bool = False
    """Day sums imported from elsewhere (e.g. a replay of older data), not measured here."""
    hours: tuple[tuple[int, float, float], ...] = ()
    """``(local hour, actual kWh, forecast kWh)`` of the used intervals, per clock hour."""

    @property
    def daylight(self) -> int:
        """Number of intervals with forecast or production."""
        return self.intervals + self.curtailed + self.missing

    @property
    def complete(self) -> bool:
        """Usable for the level factor."""
        if self.imported:
            return self.forecast_kwh > 0 and self.actual_kwh >= 0
        return (
            self.intervals > 0
            and self.forecast_kwh > 0
            and self.missing <= MAX_MISSING_SHARE * self.daylight
        )

    @property
    def ratio(self) -> float | None:
        """Measured over forecast energy of this day."""
        return self.actual_kwh / self.forecast_kwh if self.forecast_kwh > 0 else None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "day": self.day.isoformat(),
            "actual_kwh": round(self.actual_kwh, 4),
            "forecast_kwh": round(self.forecast_kwh, 4),
            "intervals": self.intervals,
            "curtailed": self.curtailed,
            "missing": self.missing,
        }
        if self.imported:
            data["imported"] = True
        if self.hours:
            data["hours"] = [[h, round(a, 4), round(f, 4)] for h, a, f in self.hours]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DayStats:
        return cls(
            day=date.fromisoformat(data["day"]),
            actual_kwh=float(data["actual_kwh"]),
            forecast_kwh=float(data["forecast_kwh"]),
            intervals=int(data["intervals"]),
            curtailed=int(data["curtailed"]),
            missing=int(data["missing"]),
            imported=bool(data.get("imported", False)),
            hours=tuple((int(h), float(a), float(f)) for h, a, f in data.get("hours", [])),
        )

    @classmethod
    def imported_sums(cls, day: date, actual_kwh: float, forecast_kwh: float) -> DayStats:
        """A day known only by its sums: measured and forecast-of-record energy."""
        return cls(day, actual_kwh, forecast_kwh, 0, 0, 0, imported=True)


def day_stats(
    day: date,
    intervals: Iterable[
        tuple[float | None, IntervalActual] | tuple[float | None, IntervalActual, int]
    ],
) -> DayStats:
    """Summarise a day from ``(forecast of record in kW, measured[, local hour])`` per
    interval.

    Intervals without a forecast are ignored unless production was measured in them
    (night, or a sensor that is silent at night). A missing forecast of record next to
    measured production, or a forecast interval without enough readings, counts as
    missing data. With the local hour, the used sums are also kept per clock hour.
    """
    actual = forecast = 0.0
    used = curtailed = missing = 0
    per_hour: dict[int, list[float]] = {}
    for record_kw, measured, *rest in intervals:
        has_forecast = record_kw is not None and record_kw > 0
        if not has_forecast and (measured.energy_kwh <= 0 or not measured.measured):
            continue
        if record_kw is None or not measured.measured:
            missing += 1
        elif measured.curtailed:
            curtailed += 1
        else:
            used += 1
            actual += measured.energy_kwh
            forecast += record_kw * INTERVAL_HOURS
            if rest:
                sums = per_hour.setdefault(int(rest[0]), [0.0, 0.0])
                sums[0] += measured.energy_kwh
                sums[1] += record_kw * INTERVAL_HOURS
    hours = tuple((h, a, f) for h, (a, f) in sorted(per_hour.items()))
    return DayStats(day, actual, forecast, used, curtailed, missing, hours=hours)


@dataclass(frozen=True, slots=True)
class LevelFactor:
    """Result of layer A, with everything needed to explain it."""

    factor: float = 1.0
    days_used: int = 0
    sum_actual: float = 0.0
    sum_forecast: float = 0.0
    shrink_weight: float = 0.0
    computed_at: datetime | None = None
    clamped: bool = False
    days: tuple[date, ...] = field(default=())
    curve: tuple[tuple[int, float], ...] = field(default=())
    """Factor per local clock hour, when learned; other hours use ``factor``."""

    def at(self, hour: int | None) -> float:
        """Factor for an interval in the given local clock hour."""
        if hour is None or not self.curve:
            return self.factor
        return dict(self.curve).get(hour, self.factor)

    @property
    def raw_ratio(self) -> float | None:
        """Unshrunk ratio of the used days."""
        return self.sum_actual / self.sum_forecast if self.sum_forecast > 0 else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor": self.factor,
            "days_used": self.days_used,
            "sum_actual": self.sum_actual,
            "sum_forecast": self.sum_forecast,
            "shrink_weight": self.shrink_weight,
            "computed_at": None if self.computed_at is None else self.computed_at.isoformat(),
            "clamped": self.clamped,
            "days": [d.isoformat() for d in self.days],
            "curve": [[h, f] for h, f in self.curve],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LevelFactor:
        computed = data.get("computed_at")
        return cls(
            factor=float(data["factor"]),
            days_used=int(data["days_used"]),
            sum_actual=float(data["sum_actual"]),
            sum_forecast=float(data["sum_forecast"]),
            shrink_weight=float(data["shrink_weight"]),
            computed_at=None if computed is None else datetime.fromisoformat(computed),
            clamped=bool(data.get("clamped", False)),
            days=tuple(date.fromisoformat(d) for d in data.get("days", [])),
            curve=tuple((int(h), float(f)) for h, f in data.get("curve", [])),
        )


def compute_level_factor(
    history: Iterable[DayStats],
    today: date,
    settings: LevelSettings,
    computed_at: datetime | None = None,
) -> LevelFactor:
    """Level factor for ``today`` from the complete days before it."""
    eligible = sorted(
        (
            stats
            for stats in history
            if stats.day < today
            and stats.complete
            and (today - stats.day).days <= settings.max_age_days
        ),
        key=lambda stats: stats.day,
    )
    used = eligible[-settings.days :] if settings.days > 0 else []
    if not used:
        return LevelFactor(computed_at=computed_at)
    sum_actual = sum(s.actual_kwh for s in used)
    sum_forecast = sum(s.forecast_kwh for s in used)
    weight = settings.shrink_days * sum_forecast / len(used)
    raw = (sum_actual + weight) / (sum_forecast + weight)
    factor = min(max(raw, settings.min_factor), settings.max_factor)
    curve = _time_curve(used, factor, settings) if settings.by_time else ()
    return LevelFactor(
        factor=factor,
        days_used=len(used),
        sum_actual=sum_actual,
        sum_forecast=sum_forecast,
        shrink_weight=weight,
        computed_at=computed_at,
        clamped=factor != raw,
        days=tuple(s.day for s in used),
        curve=curve,
    )


def _time_curve(
    used: list[DayStats], factor: float, settings: LevelSettings
) -> tuple[tuple[int, float], ...]:
    """Factor per local clock hour, shrunk toward the day factor."""
    sums: dict[int, list[float]] = {}
    for stats in used:
        for hour, actual, forecast in stats.hours:
            if forecast <= 0:
                continue
            entry = sums.setdefault(hour, [0.0, 0.0, 0.0])
            entry[0] += actual
            entry[1] += forecast
            entry[2] += 1
    if not sums:
        return ()
    mean_hour = sum(forecast / days for _, forecast, days in sums.values()) / len(sums)
    weight = settings.time_shrink_days * mean_hour
    curve = []
    for hour in sorted(sums):
        actual, forecast, _ = sums[hour]
        value = (actual + weight * factor) / (forecast + weight)
        curve.append((hour, min(max(value, settings.min_factor), settings.max_factor)))
    return tuple(curve)
