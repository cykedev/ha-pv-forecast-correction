"""The correction engine: one object that turns base forecast and measurements into the
corrected forecast. The Home Assistant integration and the replay tool drive the same
class, so what is replayed is what runs.

Typical cycle (every few minutes):

    engine.set_base(forecast, now)          # when the base forecast entities change
    engine.observe_energy(now, kwh)         # or observe_power / set_bin
    engine.observe_curtailment(now, on)
    engine.update(now)                      # day roll-over, forecast of record, fog layer
    result = engine.result(now)             # corrected forecast for the sensors
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from dataclasses import replace as replace_stats
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

from .actuals import BinStore, CurtailmentTracker, EnergyMeter, IntervalActual, PowerMeter
from .fog import FogInputs, FogMode, FogReason, FogSettings, FogState, evaluate
from .forecast_format import Forecast, ForecastPoint, energy
from .level import DayStats, LevelFactor, LevelSettings, compute_level_factor, day_stats
from .score import (
    BASE,
    CAPTURE_WINDOW,
    CORRECTED,
    DAY_AHEAD_CHECKPOINT,
    DAY_CHECKPOINT,
    REST_CHECKPOINTS,
    SHADOW,
    WEATHER,
    Band,
    DayCheckpoints,
    DayReport,
    build_report,
    checkpoint_key,
)
from .timeline import (
    INTERVAL,
    INTERVAL_HOURS,
    as_utc,
    day_bounds,
    floor_to,
    local_day,
    local_time,
    overlap_seconds,
    steps,
)
from .weather import (
    HOUR,
    WeatherForecast,
    WeatherMode,
    WeatherPoint,
    WeatherSettings,
    WeatherTypical,
    hour_factor,
    hourly_pairs,
    local_hour,
    typical_ratio,
)

STORAGE_VERSION = 1
HISTORY_DAYS = 60
"""Day statistics older than this are dropped."""
PLAUSIBLE_PEAK_FACTOR = 3.0
"""An energy reading may imply at most this multiple of the forecast's peak power ..."""
PLAUSIBLE_MIN_KW = 5.0
"""... and is always allowed up to this average power."""


@dataclass(frozen=True, slots=True)
class Settings:
    """All parameters of the correction."""

    level_enabled: bool = True
    level: LevelSettings = field(default_factory=LevelSettings)
    fog: FogSettings = field(default_factory=FogSettings)
    weather: WeatherSettings = field(default_factory=WeatherSettings)


@dataclass(frozen=True, slots=True)
class BaseUpdate:
    """Outcome of feeding a new base forecast."""

    refreshed: bool
    """Values of intervals that were already known changed (a new forecast run)."""

    changed: bool
    """Anything changed, including intervals that were added (e.g. a new day)."""


@dataclass(frozen=True, slots=True)
class DayForecast:
    """Corrected forecast for a span of a day, next to the base it came from."""

    day: date
    points: tuple[ForecastPoint, ...]
    estimate: float
    estimate10: float | None
    estimate90: float | None
    base_estimate: float
    base_estimate10: float | None
    base_estimate90: float | None


@dataclass(frozen=True, slots=True)
class FogStatus:
    """Fog layer as published."""

    mode: FogMode
    gate: str
    applied: float
    shadow: float | None
    state: FogState


@dataclass(frozen=True, slots=True)
class WeatherHour:
    """One hour of today or tomorrow: the corrected forecast with and without the weather
    factor, and what MOSMIX expects."""

    start: datetime
    pv_corrected: float
    """Mean power (kW) of the corrected forecast without the weather factor."""
    pv_estimate: float
    """The same with the weather factor."""
    factor: float | None
    sunshine: float | None
    fog: float | None
    low_cloud: float | None


@dataclass(frozen=True, slots=True)
class WeatherStatus:
    """Weather layer as published."""

    mode: WeatherMode
    station: str | None
    issued_at: datetime | None
    valid: bool
    typical: WeatherTypical
    learned: bool
    """The typical ratio comes from past days (else from the current forecasts)."""
    rest_factor: float | None = None
    tomorrow_factor: float | None = None
    rest_estimate: float | None = None
    """Corrected rest of today with the weather factor (kWh), applied or not."""
    tomorrow_estimate: float | None = None
    hours_today: dict[str, float] = field(default_factory=dict)
    hours_tomorrow: dict[str, float] = field(default_factory=dict)
    sunshine_today: float | None = None
    sunshine_tomorrow: float | None = None
    fog_today: float | None = None
    fog_tomorrow: float | None = None
    hours: tuple[WeatherHour, ...] = ()


@dataclass(frozen=True, slots=True)
class Result:
    """Everything the sensors publish."""

    now: datetime
    today: DayForecast | None
    remaining: DayForecast | None
    tomorrow: DayForecast | None
    actual_so_far: float
    level: LevelFactor
    level_applied: float
    fog: FogStatus
    report: DayReport | None
    base_valid: bool
    base_refreshed_at: datetime | None
    base_failures_today: int
    history: tuple[DayStats, ...] = field(default=())
    weather: WeatherStatus | None = None


class Corrector:
    """Applies layer A (level) and layer B (fog) to a base forecast."""

    def __init__(self, settings: Settings, tz: tzinfo) -> None:
        self.settings = settings
        self.tz = tz
        self.bins = BinStore()
        self._energy_meter = EnergyMeter(self.bins)
        self._power_meter = PowerMeter(self.bins)
        self._curtailment = CurtailmentTracker(self.bins)
        self.base = Forecast()
        self.base_valid = False
        self.base_refreshed_at: datetime | None = None
        self.base_failures_today = 0
        self.day: date | None = None
        self.record: dict[datetime, ForecastPoint] = {}
        self.history: dict[date, DayStats] = {}
        self.level = LevelFactor()
        self.fog = FogState(reason=self._initial_fog_reason())
        self.checkpoints = DayCheckpoints()
        self.next_day_ahead: tuple[date, dict[str, Band]] | None = None
        self.report: DayReport | None = None
        self.last_update: datetime | None = None
        self.meter_source: str | None = None
        """Identifies the measurement input the meter readings belong to."""
        self.weather: WeatherForecast | None = None
        self.weather_valid = False
        self.weather_record: dict[datetime, WeatherPoint] = {}
        """MOSMIX values as they stood when each hour began."""
        self.weather_factor_record: dict[datetime, float] = {}
        """Weather factor in effect when each hour began."""
        self.weather_typical = WeatherTypical()
        self._weather_factors: dict[datetime, float] = {}
        self._weather_typical_used = WeatherTypical()

    # ------------------------------------------------------------------ inputs

    def set_base(self, forecast: Forecast, now: datetime) -> BaseUpdate:
        """Replace the base forecast (today and tomorrow merged into one list)."""
        if not forecast:
            self.base_unavailable(now)
            return BaseUpdate(refreshed=False, changed=False)
        today = local_day(now, self.tz)
        if self.day is not None and today > self.day:
            # Close the previous day with the base it had, before the new one replaces it
            # (after an outage the new base no longer covers the intervals it missed).
            self._roll_over(today, as_utc(now))
        old = self.base.as_dict()
        new = forecast.as_dict()
        refreshed = not old or any(
            start in old and old[start] != point for start, point in new.items()
        )
        changed = refreshed or new.keys() != old.keys()
        if refreshed:
            self.base_refreshed_at = as_utc(now)
        self.base = forecast
        self.base_valid = True
        peak = max(p.p50 if p.p90 is None else max(p.p50, p.p90) for p in forecast.points)
        self._energy_meter.max_power_kw = max(PLAUSIBLE_MIN_KW, PLAUSIBLE_PEAK_FACTOR * peak)
        return BaseUpdate(refreshed=refreshed, changed=changed)

    def base_unavailable(self, now: datetime) -> None:
        """The base forecast could not be read; the last valid one is kept."""
        if self.base_valid:
            self.base_failures_today += 1
        self.base_valid = False

    def set_weather(self, forecast: WeatherForecast, now: datetime) -> bool:
        """Replace the weather forecast; True when it is a new run."""
        new = self.weather is None or (
            forecast.issued_at,
            forecast.station,
        ) != (self.weather.issued_at, self.weather.station)
        self.weather = forecast
        self.weather_valid = True
        self._refresh_weather_factors()
        return new

    def weather_unavailable(self) -> None:
        """The weather forecast could not be fetched; the last one is kept."""
        self.weather_valid = False

    def observe_energy(
        self, when: datetime, kwh: float | None, seen: datetime | None = None
    ) -> None:
        """Reading of a cumulative energy sensor in kWh: ``when`` the value last changed,
        ``seen`` when it was last confirmed (defaults to ``when``)."""
        self._energy_meter.observe(when, kwh, seen)

    def observe_power(self, now: datetime, kw: float | None) -> None:
        """Reading of a power sensor (or the sum of several) in kW."""
        self._power_meter.observe(now, kw)

    def observe_curtailment(self, now: datetime, active: bool | None) -> None:
        """State of a curtailment signal."""
        self._curtailment.observe(now, active)

    def set_bin(
        self, start: datetime, kwh: float, covered_s: float, curtailed: bool, gap_s: float = 0.0
    ) -> None:
        """Feed one measured 5-minute bin directly (replay)."""
        self.bins.set_bin(start, kwh, covered_s, curtailed, gap_s)

    def use_meter_source(self, source: str) -> None:
        """Forget the last meter readings if they came from a different input."""
        if self.meter_source != source:
            self._energy_meter = EnergyMeter(
                self.bins, max_power_kw=self._energy_meter.max_power_kw
            )
            self._power_meter = PowerMeter(self.bins)
            self.meter_source = source

    def measured_until(self, now: datetime) -> datetime:
        """Measurements before this instant are final. An energy counter that steps every
        few minutes has not yet reported the energy of the minutes since its last step."""
        horizon = as_utc(now)
        for meter in (self._energy_meter, self._power_meter):
            settled = meter.settled_until
            if settled is not None:
                horizon = min(horizon, settled)
        return horizon

    @property
    def rejected_readings(self) -> int:
        """Energy readings rejected as implausible since start-up."""
        return self._energy_meter.rejected

    def expects_production(self, now: datetime) -> bool:
        """True unless the base forecast says nothing is produced right now."""
        now = as_utc(now)
        for point in self.base.points:
            if point.start <= now < point.start + INTERVAL:
                return point.p50 > 0
        return True

    # ------------------------------------------------------------------ cycle

    @property
    def level_applied(self) -> float:
        return self.level.factor if self.settings.level_enabled else 1.0

    def level_at(self, start: datetime) -> float:
        """Level factor applied to the interval starting at ``start`` (time-of-day curve
        when learned, else the day factor; 1.0 when layer A is off)."""
        if not self.settings.level_enabled:
            return 1.0
        return self.level.at(as_utc(start).astimezone(self.tz).hour)

    @property
    def fog_applied(self) -> float:
        return self.fog.k if self.settings.fog.mode is FogMode.ON else 1.0

    @property
    def fog_shadow(self) -> float | None:
        return self.fog.k if self.settings.fog.mode is FogMode.SHADOW else None

    def weather_at(self, start: datetime) -> float:
        """Weather factor for the interval starting at ``start`` (applied or not)."""
        return self._weather_factors.get(floor_to(as_utc(start), HOUR), 1.0)

    def weather_applied(self, start: datetime) -> float:
        if self.settings.weather.mode is not WeatherMode.ON:
            return 1.0
        return self.weather_at(start)

    def _base_hourly(self, points: Iterable[ForecastPoint]) -> dict[datetime, float]:
        hourly: dict[datetime, float] = {}
        for point in points:
            hour = floor_to(point.start, HOUR)
            hourly[hour] = hourly.get(hour, 0.0) + point.p50 * INTERVAL_HOURS
        return hourly

    def _refresh_weather_factors(self) -> None:
        """Weather factor per hour of today and tomorrow from the current forecasts."""
        settings = self.settings.weather
        self._weather_factors = {}
        if settings.mode is WeatherMode.OFF or self.weather is None or not self.base:
            return
        weather = self.weather.as_dict()
        typical = self.weather_typical
        if typical.days_used == 0:
            # Nothing learned yet: how the two forecasts relate today and tomorrow.
            pairs = hourly_pairs(self._base_hourly(self.base.points), weather, self.tz)
            typical = typical_ratio([(date.min, pairs)], settings) if pairs else typical
        self._weather_typical_used = typical
        by_day: dict[date, dict[datetime, float]] = {}
        for hour, kwh in self._base_hourly(self.base.points).items():
            by_day.setdefault(local_day(hour + HOUR / 2, self.tz), {})[hour] = kwh
        for hours in by_day.values():
            peak = max(hours.values(), default=0.0)
            for hour, kwh in hours.items():
                point = weather.get(hour)
                if point is None or kwh < settings.min_share * peak:
                    continue
                self._weather_factors[hour] = hour_factor(
                    point.irradiance, kwh, typical.at(local_hour(hour, self.tz)), settings
                )

    def update(self, now: datetime) -> None:
        """Advance the state to ``now``."""
        now = as_utc(now)
        today = local_day(now, self.tz)
        if self.day is None:
            self.day = today
            self._compute_level(now)
        elif today < self.day:
            return  # clock went backwards; keep the state of the later day
        elif today > self.day:
            self._roll_over(today, now)
        self._refresh_weather_factors()
        self._record(now)
        self._update_fog(now)
        self._capture_checkpoints(now)
        self.last_update = now

    def recompute_level(self, now: datetime) -> None:
        """Recompute the level factor now (after settings changed or a reset)."""
        self._compute_level(as_utc(now))

    def import_days(
        self,
        days: Iterable[tuple[date, float, float] | tuple[date, float, float, tuple]],
        now: datetime,
        *,
        replace: bool = False,
    ) -> int:
        """Learn from days known only by their sums ``(day, measured kWh, forecast kWh)``,
        optionally with sums per clock hour ``((hour, measured, forecast), ...)``, for
        example from the recorder's history. Days measured here win unless ``replace``;
        days imported earlier are replaced. Returns the number of days taken over and
        recomputes the level factor. A measured day that is incomplete (for example the day of
        installation) does not block an import of the same day."""
        today = self.day if self.day is not None else local_day(now, self.tz)
        taken = 0
        for day, actual, forecast, *rest in days:
            if day >= today or forecast <= 0 or actual < 0:
                continue
            existing = self.history.get(day)
            if existing is not None and not existing.imported and existing.complete and not replace:
                continue
            hours = tuple(rest[0]) if rest else ()
            self.history[day] = replace_stats(
                DayStats.imported_sums(day, actual, forecast), hours=hours
            )
            taken += 1
        if self.day is None:
            self.day = today
        self._compute_level(as_utc(now))
        return taken

    def reset_learning(self) -> None:
        """Forget the day statistics; the level factor returns to neutral."""
        self.history.clear()
        self.level = LevelFactor(computed_at=self.level.computed_at)

    def _initial_fog_reason(self) -> FogReason:
        if self.settings.fog.mode is FogMode.OFF:
            return FogReason.DISABLED
        return FogReason.NO_FORECAST

    def _compute_level(self, now: datetime) -> None:
        assert self.day is not None
        self.level = compute_level_factor(
            self.history.values(), self.day, self.settings.level, computed_at=now
        )
        oldest = self.day - timedelta(days=2 * self.settings.weather.days)
        self.weather_typical = typical_ratio(
            (
                (stats.day, stats.weather)
                for stats in sorted(self.history.values(), key=lambda s: s.day)
                if oldest <= stats.day < self.day and stats.weather
            ),
            self.settings.weather,
        )
        self._refresh_weather_factors()

    def _roll_over(self, today: date, now: datetime) -> None:
        previous = self.day
        assert previous is not None
        prev_start, prev_end = day_bounds(previous, self.tz)
        for point in self.base.between(prev_start, prev_end):
            self.record.setdefault(point.start, point)
        if self.weather is not None:
            for point in self.weather.between(prev_start, prev_end):
                self.weather_record.setdefault(point.start, point)
        stats = self._day_stats(previous)
        record_hourly = self._base_hourly(
            p for s, p in self.record.items() if prev_start <= s < prev_end
        )
        stats = replace_stats(
            stats, weather=hourly_pairs(record_hourly, self.weather_record, self.tz)
        )
        self.history[previous] = stats
        self.report = self._build_report(previous, stats)

        self.day = today
        start, _ = day_bounds(today, self.tz)
        self.record = {s: p for s, p in self.record.items() if s >= start}
        self.weather_record = {s: p for s, p in self.weather_record.items() if s >= start}
        self.weather_factor_record = {
            s: f for s, f in self.weather_factor_record.items() if s >= start
        }
        cutoff = today - timedelta(days=HISTORY_DAYS)
        self.history = {d: s for d, s in self.history.items() if d >= cutoff}
        self._compute_level(now)
        self.fog = FogState(reason=self._initial_fog_reason())
        self.checkpoints = DayCheckpoints()
        if self.next_day_ahead is not None:
            target, bands = self.next_day_ahead
            if target == today:
                self.checkpoints.day_ahead = bands
            if target <= today:
                self.next_day_ahead = None
        self.bins.prune(prev_start)
        self.base_failures_today = 0

    def _record(self, now: datetime) -> None:
        assert self.day is not None
        start, end = day_bounds(self.day, self.tz)
        for point in self.base.points:
            if point.start > now or point.start >= end:
                break
            if point.start >= start:
                self.record.setdefault(point.start, point)
        if self.weather is not None:
            for weather_point in self.weather.points:
                if start <= weather_point.start <= now and weather_point.start < end:
                    self.weather_record.setdefault(weather_point.start, weather_point)
            for hour in steps(floor_to(start, HOUR), min(now, end), HOUR):
                if hour >= start - HOUR / 2:
                    self.weather_factor_record.setdefault(hour, self.weather_applied(hour))

    def _day_stats(self, day: date) -> DayStats:
        start, end = day_bounds(day, self.tz)
        pairs = []
        for interval in steps(start, end, INTERVAL):
            point = self.record.get(interval)
            pairs.append(
                (
                    None if point is None else point.p50,
                    self.bins.interval(interval),
                    interval.astimezone(self.tz).hour,
                )
            )
        return day_stats(day, pairs)

    def _today_points(self) -> list[ForecastPoint]:
        """Today's intervals: forecast of record where known, else the current base."""
        assert self.day is not None
        start, end = day_bounds(self.day, self.tz)
        merged = dict(self.record)
        for point in self.base.between(start, end):
            merged.setdefault(point.start, point)
        return [merged[s] for s in sorted(merged) if start <= s < end]

    def _fog_inputs(self, now: datetime) -> FogInputs:
        assert self.day is not None
        start, end = day_bounds(self.day, self.tz)
        horizon = self.measured_until(now)
        completed = [i for i in steps(start, end, INTERVAL) if i + INTERVAL <= horizon]
        recent_start = (
            completed[-1] + INTERVAL - timedelta(hours=self.settings.fog.recent_hours)
            if completed
            else horizon
        )
        expected_morning = actual_morning = expected_recent = actual_recent = 0.0
        for interval in completed:
            point = self.record.get(interval)
            if point is None or point.p50 <= 0:
                continue
            measured = self.bins.interval(interval)
            if not measured.measured or measured.curtailed:
                continue
            expected = (
                self.level_at(interval)
                * self.weather_factor_record.get(floor_to(interval, HOUR), 1.0)
                * point.p50
                * INTERVAL_HOURS
            )
            expected_morning += expected
            actual_morning += measured.energy_kwh
            if interval >= recent_start:
                expected_recent += expected
                actual_recent += measured.energy_kwh
        expected_day = (
            energy(
                p.scaled(self.level_at(p.start) * self.weather_applied(p.start))
                for p in self._today_points()
            )
            or 0.0
        )
        today_base = self.base.between(start, end)
        confidence = None
        base_p50 = energy(today_base) or 0.0
        base_p10 = energy(today_base, "p10")
        if base_p10 is not None and base_p50 > 0:
            confidence = base_p10 / base_p50
        return FogInputs(
            expected_morning=expected_morning,
            actual_morning=actual_morning,
            expected_recent=expected_recent,
            actual_recent=actual_recent,
            expected_day=expected_day,
            confidence=confidence,
        )

    def _update_fog(self, now: datetime) -> None:
        if self.settings.fog.mode is FogMode.OFF:
            self.fog = FogState(reason=FogReason.DISABLED)
            return
        self.fog = evaluate(self.fog, self._fog_inputs(now), self.settings.fog, now)

    # ------------------------------------------------------------------ checkpoints

    def _base_band(
        self,
        points: Iterable[ForecastPoint],
        start: datetime,
        end: datetime,
        scale: Callable[[datetime], float] | None = None,
    ) -> Band:
        """Energy of the base between ``start`` and ``end`` (partial intervals pro rata),
        optionally scaled per interval."""
        p50 = 0.0
        p10: float | None = 0.0
        p90: float | None = 0.0
        for point in points:
            seconds = overlap_seconds(point.start, point.start + INTERVAL, start, end)
            if seconds <= 0:
                continue
            hours = seconds / 3600 * (1.0 if scale is None else scale(point.start))
            p50 += point.p50 * hours
            p10 = None if p10 is None or point.p10 is None else p10 + point.p10 * hours
            p90 = None if p90 is None or point.p90 is None else p90 + point.p90 * hours
        return Band(p50, p10, p90)

    def _variants(
        self, points: list[ForecastPoint], start: datetime, end: datetime, measured: float = 0.0
    ) -> dict[str, Band]:
        """Base, corrected and (in shadow mode) shadow version of the base between ``start``
        and ``end``; ``measured`` is added to the corrected variants."""
        corrected = self._base_band(points, start, end, self._corrected_scale)
        bands = {
            BASE: self._base_band(points, start, end),
            CORRECTED: corrected.scaled(self.fog_applied, measured),
        }
        shadow = self.fog_shadow
        if shadow is not None:
            bands[SHADOW] = corrected.scaled(shadow, measured)
        if self._weather_in_shadow:
            with_weather = self._base_band(points, start, end, self._weather_scale)
            bands[WEATHER] = with_weather.scaled(self.fog_applied, measured)
        return bands

    def _corrected_scale(self, start: datetime) -> float:
        return self.level_at(start) * self.weather_applied(start)

    def _weather_scale(self, start: datetime) -> float:
        return self.level_at(start) * self.weather_at(start)

    @property
    def _weather_in_shadow(self) -> bool:
        return self.settings.weather.mode is WeatherMode.SHADOW and bool(self._weather_factors)

    def _capture_checkpoints(self, now: datetime) -> None:
        assert self.day is not None
        start, end = day_bounds(self.day, self.tz)
        today_base = self.base.between(start, end)

        nominal = local_time(self.day, DAY_CHECKPOINT, self.tz)
        if (
            today_base
            and not self.checkpoints.day_total
            and nominal <= now < nominal + CAPTURE_WINDOW
        ):
            until = max(start, min(nominal, self.measured_until(now)))
            measured = self.bins.actual(start, until).energy_kwh
            variants = self._variants(today_base, until, end, measured)
            variants[BASE] = self._base_band(today_base, start, end)
            self.checkpoints.day_total = variants

        for wall in REST_CHECKPOINTS:
            key = checkpoint_key(wall)
            nominal = local_time(self.day, wall, self.tz)
            if key in self.checkpoints.rest or not nominal <= now < nominal + CAPTURE_WINDOW:
                continue
            if today_base:
                self.checkpoints.rest[key] = self._variants(today_base, nominal, end)

        nominal = local_time(self.day, DAY_AHEAD_CHECKPOINT, self.tz)
        tomorrow = self.day + timedelta(days=1)
        if nominal <= now < nominal + CAPTURE_WINDOW and (
            self.next_day_ahead is None or self.next_day_ahead[0] != tomorrow
        ):
            t_start, t_end = day_bounds(tomorrow, self.tz)
            points = self.base.between(t_start, t_end)
            if points:
                bands = {
                    BASE: self._base_band(points, t_start, t_end),
                    CORRECTED: self._base_band(points, t_start, t_end, self._corrected_scale),
                }
                if self._weather_in_shadow:
                    bands[WEATHER] = self._base_band(points, t_start, t_end, self._weather_scale)
                self.next_day_ahead = (tomorrow, bands)

    def _build_report(self, day: date, stats: DayStats) -> DayReport:
        start, end = day_bounds(day, self.tz)
        actual = self.bins.actual(start, end).energy_kwh
        actual_rest = {
            key: self.bins.actual(local_time(day, wall, self.tz), end).energy_kwh
            for wall in REST_CHECKPOINTS
            if (key := checkpoint_key(wall)) in self.checkpoints.rest
        }
        return build_report(
            day,
            self.checkpoints,
            actual,
            actual_rest,
            complete=stats.complete,
            level_factor=self.level_applied,
            fog_active=self.fog.active,
        )

    # ------------------------------------------------------------------ output

    def _day_forecast(
        self, day: date, points: list[ForecastPoint], base_points: list[ForecastPoint]
    ) -> DayForecast:
        bands = all(p.p10 is not None and p.p90 is not None for p in base_points)
        return DayForecast(
            day=day,
            points=tuple(points),
            estimate=energy(points) or 0.0,
            estimate10=energy(points, "p10") if bands else None,
            estimate90=energy(points, "p90") if bands else None,
            base_estimate=energy(base_points) or 0.0,
            base_estimate10=energy(base_points, "p10") if bands else None,
            base_estimate90=energy(base_points, "p90") if bands else None,
        )

    @staticmethod
    def _blend(forecast: ForecastPoint, measured: IntervalActual, left: float) -> ForecastPoint:
        """Mean power of an interval: measured energy plus the forecast for the time that
        is still ahead (``left``) or whose energy is not known at all."""
        elapsed = 1.0 - left
        share = left + elapsed * (1.0 - measured.known)
        measured_kw = measured.energy_kwh / INTERVAL_HOURS

        def value(band: float | None) -> float | None:
            return None if band is None else measured_kw + band * share

        return ForecastPoint(
            forecast.start,
            measured_kw + forecast.p50 * share,
            value(forecast.p10),
            value(forecast.p90),
        )

    def _measured_blend(
        self, forecast: ForecastPoint, until: datetime, horizon: datetime
    ) -> ForecastPoint:
        """Interval with its measurements up to ``until`` (as far as they are final) and
        the forecast for the rest."""
        until = max(forecast.start, min(until, horizon))
        left = (forecast.start + INTERVAL - until) / INTERVAL
        return self._blend(forecast, self.bins.actual(forecast.start, until), left)

    def result(self, now: datetime) -> Result:
        """Corrected forecasts as of ``now`` (call ``update`` first)."""
        now = as_utc(now)
        horizon = self.measured_until(now)
        day = self.day if self.day is not None else local_day(now, self.tz)
        start, end = day_bounds(day, self.tz)
        today_base = self.base.between(start, end)
        bands = bool(today_base) and all(
            p.p10 is not None and p.p90 is not None for p in today_base
        )

        today_points: list[ForecastPoint] = []
        rest_points: list[ForecastPoint] = []
        rest_energy = Band(0.0, 0.0 if bands else None, 0.0 if bands else None)
        for point in today_base:
            interval_end = point.start + INTERVAL
            corrected = point.scaled(self._corrected_scale(point.start) * self.fog_applied)
            if interval_end <= now:
                # Past: measured where readings cover the interval, the level-corrected
                # forecast of record for the uncovered rest.
                record = self.record.get(point.start, point).scaled(self.level_at(point.start))
                today_points.append(self._measured_blend(record, interval_end, horizon))
                continue
            rest_points.append(corrected)
            left = min(1.0, (interval_end - max(now, point.start)) / INTERVAL)
            part = Band(corrected.p50, corrected.p10, corrected.p90).scaled(left * INTERVAL_HOURS)
            rest_energy = Band(
                rest_energy.p50 + part.p50,
                None if rest_energy.p10 is None or part.p10 is None else rest_energy.p10 + part.p10,
                None if rest_energy.p90 is None or part.p90 is None else rest_energy.p90 + part.p90,
            )
            if point.start < now:
                today_points.append(self._measured_blend(corrected, now, horizon))
            else:
                today_points.append(corrected)

        today = remaining = None
        if today_base:
            today = self._day_forecast(day, today_points, today_base)
            base_rest = self._base_band(today_base, max(now, start), end)
            remaining = DayForecast(
                day=day,
                points=tuple(rest_points),
                estimate=rest_energy.p50,
                estimate10=rest_energy.p10,
                estimate90=rest_energy.p90,
                base_estimate=base_rest.p50,
                base_estimate10=base_rest.p10,
                base_estimate90=base_rest.p90,
            )

        tomorrow = None
        tomorrow_day = day + timedelta(days=1)
        t_start, t_end = day_bounds(tomorrow_day, self.tz)
        tomorrow_base = self.base.between(t_start, t_end)
        if tomorrow_base:
            tomorrow = self._day_forecast(
                tomorrow_day,
                [p.scaled(self._corrected_scale(p.start)) for p in tomorrow_base],
                tomorrow_base,
            )

        return Result(
            now=now,
            today=today,
            remaining=remaining,
            tomorrow=tomorrow,
            actual_so_far=self.bins.actual(start, min(now, end)).energy_kwh,
            level=self.level,
            level_applied=self.level_applied,
            fog=FogStatus(
                mode=self.settings.fog.mode,
                gate=self.settings.fog.gate.name,
                applied=self.fog_applied,
                shadow=self.fog_shadow,
                state=self.fog,
            ),
            report=self.report,
            base_valid=self.base_valid,
            base_refreshed_at=self.base_refreshed_at,
            base_failures_today=self.base_failures_today,
            history=tuple(self.history[d] for d in sorted(self.history)),
            weather=self._weather_status(now, today_base, tomorrow_base),
        )

    def _weather_status(
        self,
        now: datetime,
        today_base: list[ForecastPoint],
        tomorrow_base: list[ForecastPoint],
    ) -> WeatherStatus:
        settings = self.settings.weather
        status = WeatherStatus(
            mode=settings.mode,
            station=settings.station,
            issued_at=None if self.weather is None else self.weather.issued_at,
            valid=self.weather_valid,
            typical=self._weather_typical_used,
            learned=self.weather_typical.days_used > 0,
        )
        if settings.mode is WeatherMode.OFF or not self._weather_factors:
            return status
        weather = {} if self.weather is None else self.weather.as_dict()

        def summary(
            points: list[ForecastPoint], start: datetime, fog_factor: float
        ) -> tuple[Any, ...]:
            plain = with_weather = 0.0
            hours: dict[str, float] = {}
            for point in points:
                end = point.start + INTERVAL
                hours_left = max(0.0, (end - max(start, point.start)).total_seconds()) / 3600
                scale = self.level_at(point.start) * fog_factor
                plain += point.p50 * scale * hours_left
                with_weather += point.p50 * scale * self.weather_at(point.start) * hours_left
                hour = floor_to(point.start, HOUR)
                if hour in self._weather_factors:
                    label = f"{local_hour(hour, self.tz):02d}:00"
                    hours[label] = round(self._weather_factors[hour], 3)
            factor = with_weather / plain if plain > 0 else None
            hour_starts = {floor_to(p.start, HOUR) for p in points}
            sunshine = [weather[h].sunshine for h in hour_starts if h in weather]
            fog = [weather[h].fog for h in hour_starts if h in weather]
            return (
                factor,
                with_weather,
                hours,
                sum(v for v in sunshine if v is not None) if sunshine else None,
                max((v for v in fog if v is not None), default=None),
            )

        rest = [p for p in today_base if p.start + INTERVAL > now]
        start = max(now, rest[0].start) if rest else now
        r_factor, r_estimate, _, r_sun, r_fog = summary(rest, start, self.fog_applied)
        t_start = tomorrow_base[0].start if tomorrow_base else now
        t_factor, t_estimate, t_hours, t_sun, t_fog = summary(tomorrow_base, t_start, 1.0)
        today_hours = (
            summary(today_base, today_base[0].start, self.fog_applied)[2] if today_base else {}
        )
        hourly: dict[datetime, list[float]] = {}
        for points, fog_factor in ((today_base, self.fog_applied), (tomorrow_base, 1.0)):
            for point in points:
                kwh = point.p50 * self.level_at(point.start) * fog_factor * INTERVAL_HOURS
                sums = hourly.setdefault(floor_to(point.start, HOUR), [0.0, 0.0])
                sums[0] += kwh
                sums[1] += kwh * self.weather_at(point.start)
        hours = tuple(
            WeatherHour(
                start=hour,
                pv_corrected=corrected,
                pv_estimate=with_weather,
                factor=self._weather_factors.get(hour),
                sunshine=None if hour not in weather else weather[hour].sunshine,
                fog=None if hour not in weather else weather[hour].fog,
                low_cloud=None if hour not in weather else weather[hour].low_cloud,
            )
            for hour, (corrected, with_weather) in sorted(hourly.items())
        )
        return replace_stats(
            status,
            rest_factor=r_factor,
            tomorrow_factor=t_factor,
            rest_estimate=r_estimate if rest else 0.0,
            tomorrow_estimate=t_estimate if tomorrow_base else None,
            hours_today=today_hours,
            hours_tomorrow=t_hours,
            sunshine_today=r_sun,
            sunshine_tomorrow=t_sun,
            fog_today=r_fog,
            fog_tomorrow=t_fog,
            hours=hours,
        )

    # ------------------------------------------------------------------ persistence

    def to_dict(self) -> dict[str, Any]:
        """State for a restart (settings are not part of it)."""

        def point_row(point: ForecastPoint) -> list[Any]:
            return [int(point.start.timestamp()), point.p50, point.p10, point.p90]

        return {
            "version": STORAGE_VERSION,
            "day": None if self.day is None else self.day.isoformat(),
            "base": [point_row(p) for p in self.base.points],
            "base_valid": self.base_valid,
            "base_refreshed_at": None
            if self.base_refreshed_at is None
            else self.base_refreshed_at.isoformat(),
            "base_failures_today": self.base_failures_today,
            "record": [point_row(self.record[s]) for s in sorted(self.record)],
            "history": [self.history[d].to_dict() for d in sorted(self.history)],
            "level": self.level.to_dict(),
            "fog": self.fog.to_dict(),
            "checkpoints": self.checkpoints.to_dict(),
            "next_day_ahead": None
            if self.next_day_ahead is None
            else [
                self.next_day_ahead[0].isoformat(),
                {name: band.to_list() for name, band in self.next_day_ahead[1].items()},
            ],
            "report": None if self.report is None else self.report.to_dict(),
            "bins": self.bins.to_dict(),
            "energy_meter": self._energy_meter.to_dict(),
            "power_meter": self._power_meter.to_dict(),
            "curtailment": self._curtailment.to_dict(),
            "meter_source": self.meter_source,
            "last_update": None if self.last_update is None else self.last_update.isoformat(),
            "weather": None if self.weather is None else self.weather.to_dict(),
            "weather_valid": self.weather_valid,
            "weather_record": [p.to_list() for _, p in sorted(self.weather_record.items())],
            "weather_factor_record": [
                [int(s.timestamp()), f] for s, f in sorted(self.weather_factor_record.items())
            ],
            "weather_typical": self.weather_typical.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], settings: Settings, tz: tzinfo) -> Corrector:
        """Restore a saved state; unknown or broken parts fall back to empty state."""
        engine = cls(settings, tz)
        if data.get("version") != STORAGE_VERSION:
            return engine

        def point(row: list[Any]) -> ForecastPoint:
            return ForecastPoint(datetime.fromtimestamp(int(row[0]), UTC), *row[1:4])

        if data.get("day"):
            engine.day = date.fromisoformat(data["day"])
        engine.base = Forecast.from_points(point(row) for row in data.get("base", []))
        engine.base_valid = bool(data.get("base_valid")) and bool(engine.base)
        if data.get("base_refreshed_at"):
            engine.base_refreshed_at = datetime.fromisoformat(data["base_refreshed_at"])
        engine.base_failures_today = int(data.get("base_failures_today", 0))
        engine.record = {p.start: p for p in (point(row) for row in data.get("record", []))}
        engine.history = {
            s.day: s for s in (DayStats.from_dict(item) for item in data.get("history", []))
        }
        if data.get("level"):
            engine.level = LevelFactor.from_dict(data["level"])
        if data.get("fog"):
            engine.fog = FogState.from_dict(data["fog"])
            if settings.fog.mode is FogMode.OFF:
                engine.fog = FogState(reason=FogReason.DISABLED)
        engine.checkpoints = DayCheckpoints.from_dict(data.get("checkpoints", {}))
        if data.get("next_day_ahead"):
            target, bands = data["next_day_ahead"]
            engine.next_day_ahead = (
                date.fromisoformat(target),
                {name: Band.from_list(values) for name, values in bands.items()},
            )
        if data.get("report"):
            engine.report = DayReport.from_dict(data["report"])
        engine.bins = BinStore.from_dict(data.get("bins", {}))
        engine._energy_meter = EnergyMeter(engine.bins)
        engine._energy_meter.restore(data.get("energy_meter", {}))
        engine._power_meter = PowerMeter(engine.bins)
        engine._power_meter.restore(data.get("power_meter", {}))
        engine._curtailment = CurtailmentTracker(engine.bins)
        engine._curtailment.restore(data.get("curtailment", {}))
        engine.meter_source = data.get("meter_source")
        if engine.base:
            peak = max(max(p.p50, p.p90 or 0.0) for p in engine.base.points)
            engine._energy_meter.max_power_kw = max(PLAUSIBLE_MIN_KW, PLAUSIBLE_PEAK_FACTOR * peak)
        if data.get("last_update"):
            engine.last_update = datetime.fromisoformat(data["last_update"])
        if data.get("weather"):
            engine.weather = WeatherForecast.from_dict(data["weather"])
            engine.weather_valid = bool(data.get("weather_valid", True))
        engine.weather_record = {
            p.start: p for p in (WeatherPoint.from_list(r) for r in data.get("weather_record", []))
        }
        engine.weather_factor_record = {
            datetime.fromtimestamp(int(s), UTC): float(f)
            for s, f in data.get("weather_factor_record", [])
        }
        if data.get("weather_typical"):
            engine.weather_typical = WeatherTypical.from_dict(data["weather_typical"])
        engine._refresh_weather_factors()
        return engine
