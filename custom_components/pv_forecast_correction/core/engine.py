"""The correction engine: one object that turns base forecast and measurements into the
corrected forecast. The Home Assistant integration and the replay tool drive the same
class, so what is replayed is what runs.

Typical cycle (every few minutes):

    engine.set_base(forecast, now)          # when the base forecast entities change
    engine.observe_energy(now, kwh)         # or observe_power / set_bin
    engine.observe_curtailment(now, on)
    engine.update(now)                      # day roll-over, forecast of record, layer B
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
from .intraday import (
    IntradaySettings,
    ShortTermReason,
    Tuning,
    TuningDay,
    fog_allowed,
    multiplier,
    tune,
)
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
    strong_hours,
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
    intraday: IntradaySettings = field(default_factory=IntradaySettings)


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
    """Layer B (fog layer and short-term part) as published."""

    mode: FogMode
    gate: str
    applied: float
    """Fog factor applied to the rest of today (1.0 when the fog layer may not be used)."""
    shadow: float | None
    state: FogState
    k_hour: float | None = None
    """Measured over expected energy in the last hour (short-term part)."""
    short_term: ShortTermReason = ShortTermReason.DISABLED
    fog_used: bool = True
    """The fog layer may carry the rest of today (nightly choice or setting)."""
    tuning: Tuning = field(default_factory=Tuning)
    next_hour_factor: float | None = None
    """Layer B factor on the next hour (applied or, in shadow mode, not)."""
    rest_factor: float | None = None
    """Layer B factor on the rest of today, energy-weighted (applied or not)."""


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
        """Weather factor (applied or not) as it stood when each hour began."""
        self.fog_weather = FogState(reason=self._initial_fog_reason())
        """The fog layer as it would run if the weather factor were applied (scores the
        weather variant in shadow mode without counting fog twice)."""
        self.weather_typical = WeatherTypical()
        self._weather_factors: dict[datetime, float] = {}
        self._weather_typical_used = WeatherTypical()
        self.k_hour: float | None = None
        """Short-term ratio against the published forecast of record."""
        self.k_hour_weather: float | None = None
        """The same against the record with the weather factor (weather variant)."""
        self.short_term = self._initial_short_term()
        self.tuning = Tuning()
        self.tuning_days: dict[date, TuningDay] = {}
        """Hourly records of past days for the nightly choice of the fog layer."""
        self.asof: dict[int, dict[int, float]] = {}
        """Today: per full local hour, the corrected forecast of the remaining hours (kWh)
        as it stood then."""

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
        # The further elements are only archived; the engine does not keep them.
        self.weather = replace_stats(
            forecast, points=tuple(replace_stats(p, extra=()) for p in forecast.points)
        )
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
        """Fog factor applied to the rest of today."""
        if self.settings.fog.mode is not FogMode.ON or not self.fog_used:
            return 1.0
        return self.fog.k

    @property
    def fog_shadow(self) -> float | None:
        """Fog factor that would apply, in shadow mode."""
        if self.settings.fog.mode is not FogMode.SHADOW:
            return None
        return self.fog.k if self.fog_used else 1.0

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
        base = self._base_hourly(self.base.points)
        # How the two forecasts relate today and tomorrow stands in for the hours that
        # have not been learned yet (all of them before the first learned day).
        pairs = hourly_pairs(base, weather, self.tz, min_share=settings.min_share)
        current = WeatherTypical()
        if pairs:
            current = replace_stats(
                typical_ratio([(date.min, pairs)], settings), days_used=0, days=()
            )
        learned = self.weather_typical
        typical = learned.filled_from(current) if learned.days_used else current
        self._weather_typical_used = typical
        for hour in strong_hours(base, self.tz, settings.min_share):
            point = weather.get(hour)
            if point is None:
                continue
            self._weather_factors[hour] = hour_factor(
                point.irradiance, base[hour], typical.at(local_hour(hour, self.tz)), settings
            )

    @property
    def weather_typical_used(self) -> WeatherTypical:
        """The typical ratio behind the current weather factors (learned hours, the rest
        from the current forecasts)."""
        return self._weather_typical_used

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
        self._update_short_term(now)
        self._capture_asof(now)
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

    def _initial_short_term(self) -> ShortTermReason:
        if self.settings.fog.mode is FogMode.OFF or not self.settings.intraday.enabled:
            return ShortTermReason.DISABLED
        return ShortTermReason.WAITING

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
        self._tune()

    def _tune(self) -> None:
        """Nightly choice whether the fog layer may carry the rest of the day."""
        assert self.day is not None
        oldest = self.day - timedelta(days=2 * self.settings.intraday.days)
        days = []
        for day in sorted(set(self.history) | set(self.tuning_days)):
            if not oldest <= day < self.day:
                continue
            record = self.tuning_days.get(day)
            if record is None:
                record = self._tuning_day_from_history(day)
            if record is not None:
                days.append(record)
        self.tuning = tune(days, self.settings.fog, self.settings.intraday)

    def _tuning_day_from_history(self, day: date) -> TuningDay | None:
        """Hourly record of a day known only from its statistics (e.g. imported): the
        forecast of record stands in for the forecast as of each hour."""
        stats = self.history.get(day)
        if stats is None or not stats.hours or not stats.complete:
            return None
        level = (
            compute_level_factor(self.history.values(), day, self.settings.level)
            if self.settings.level_enabled
            else LevelFactor()
        )
        return TuningDay(
            day=day,
            expected={h: f * level.at(h) for h, _, f in stats.hours if f > 0},
            actual={h: a for h, a, f in stats.hours if f > 0},
        )

    def _tuning_day(self, day: date, confidence: float | None) -> TuningDay:
        """Hourly record of the day that is closing: expected of record as published
        (level and applied weather factor), measured energy, forecasts as of each hour.
        Hours with an interval that was curtailed or not measured are left out of all
        three, so that the forecasts are compared with what was measured."""
        start, end = day_bounds(day, self.tz)
        weather_on = self.settings.weather.mode is WeatherMode.ON
        expected: dict[int, float] = {}
        actual: dict[int, float] = {}
        left_out: set[int] = set()
        for interval in steps(start, end, INTERVAL):
            point = self.record.get(interval)
            if point is None or point.p50 <= 0:
                continue
            measured = self.bins.interval(interval)
            if not measured.measured or measured.curtailed:
                left_out.add(interval.astimezone(self.tz).hour)
                continue
            weather = (
                self.weather_factor_record.get(floor_to(interval, HOUR), 1.0) if weather_on else 1.0
            )
            hour = interval.astimezone(self.tz).hour
            expected[hour] = expected.get(hour, 0.0) + (
                point.p50 * self.level_at(interval) * weather * INTERVAL_HOURS
            )
            actual[hour] = actual.get(hour, 0.0) + measured.energy_kwh
        for hour in left_out:
            expected.pop(hour, None)
            actual.pop(hour, None)
        asof = {
            h: {j: kwh for j, kwh in remaining.items() if j in expected}
            for h, remaining in self.asof.items()
        }
        return TuningDay(day, expected, actual, asof or None, confidence)

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
            stats,
            weather=hourly_pairs(
                record_hourly,
                self.weather_record,
                self.tz,
                min_share=self.settings.weather.min_share,
            ),
        )
        self.history[previous] = stats
        self.report = self._build_report(previous, stats)
        if stats.complete:
            self.tuning_days[previous] = self._tuning_day(previous, self.report.base_confidence)
        self.tuning_days = {
            d: t
            for d, t in self.tuning_days.items()
            if d > today - timedelta(days=2 * self.settings.intraday.days)
        }
        self.asof = {}

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
        self.fog_weather = FogState(reason=self._initial_fog_reason())
        self.k_hour = self.k_hour_weather = None
        self.short_term = self._initial_short_term()
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
                    self.weather_factor_record.setdefault(hour, self.weather_at(hour))

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

    def _fog_inputs(self, now: datetime, *, with_weather: bool) -> FogInputs:
        """What the fog layer compares: measured against the forecast of record, which
        includes the weather factor when ``with_weather``."""
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
            weather = (
                self.weather_factor_record.get(floor_to(interval, HOUR), 1.0)
                if with_weather
                else 1.0
            )
            expected = self.level_at(interval) * weather * point.p50 * INTERVAL_HOURS
            expected_morning += expected
            actual_morning += measured.energy_kwh
            if interval >= recent_start:
                expected_recent += expected
                actual_recent += measured.energy_kwh

        def scale(start: datetime) -> float:
            return self.level_at(start) * (self.weather_at(start) if with_weather else 1.0)

        expected_day = energy(p.scaled(scale(p.start)) for p in self._today_points()) or 0.0
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
            self.fog = self.fog_weather = FogState(reason=FogReason.DISABLED)
            return
        applied = self.settings.weather.mode is WeatherMode.ON
        self.fog = evaluate(
            self.fog, self._fog_inputs(now, with_weather=applied), self.settings.fog, now
        )
        if self._weather_in_shadow:
            self.fog_weather = evaluate(
                self.fog_weather, self._fog_inputs(now, with_weather=True), self.settings.fog, now
            )

    def _update_short_term(self, now: datetime) -> None:
        if self.settings.fog.mode is FogMode.OFF or not self.settings.intraday.enabled:
            self.k_hour = self.k_hour_weather = None
            self.short_term = ShortTermReason.DISABLED
            return
        applied = self.settings.weather.mode is WeatherMode.ON
        self.k_hour, self.short_term = self._short_term_ratio(now, with_weather=applied)
        self.k_hour_weather = (
            self._short_term_ratio(now, with_weather=True)[0] if self._weather_in_shadow else None
        )

    def _short_term_ratio(
        self, now: datetime, *, with_weather: bool
    ) -> tuple[float | None, ShortTermReason]:
        """Measured over expected energy in the last hour of final measurements."""
        assert self.day is not None
        start, end = day_bounds(self.day, self.tz)
        horizon = min(self.measured_until(now), end)
        window = max(start, horizon - HOUR)
        expected = actual = 0.0
        for interval in steps(floor_to(window, INTERVAL), horizon, INTERVAL):
            point = self.record.get(interval)
            lo, hi = max(interval, window), min(interval + INTERVAL, horizon)
            if point is None or point.p50 <= 0 or hi <= lo:
                continue
            measured = self.bins.actual(lo, hi)
            if not measured.measured or measured.curtailed:
                continue
            weather = (
                self.weather_factor_record.get(floor_to(interval, HOUR), 1.0)
                if with_weather
                else 1.0
            )
            hours = (hi - lo).total_seconds() / 3600
            expected += self.level_at(interval) * weather * point.p50 * hours
            actual += measured.energy_kwh

        def scale(at: datetime) -> float:
            return self.level_at(at) * (self.weather_at(at) if with_weather else 1.0)

        day_expected = energy(p.scaled(scale(p.start)) for p in self._today_points()) or 0.0
        settings = self.settings.intraday
        k = settings.ratio(actual, expected, day_expected)
        if k is not None:
            return k, ShortTermReason.ACTIVE
        if actual <= 0 < expected and expected >= settings.min_share * day_expected:
            return None, ShortTermReason.NOTHING_MEASURED
        return None, ShortTermReason.WAITING

    def _capture_asof(self, now: datetime) -> None:
        """At the first update of each full local hour with production, keep the corrected
        forecast (level and applied weather factor, without layer B) of the remaining hours
        of today."""
        assert self.day is not None
        start, end = day_bounds(self.day, self.tz)
        local = now.astimezone(self.tz)
        hour = local.hour
        hour_start = as_utc(local.replace(minute=0, second=0, microsecond=0))
        if hour in self.asof or not start <= hour_start < end or now - hour_start > CAPTURE_WINDOW:
            return
        remaining: dict[int, float] = {}
        for point in self.base.between(hour_start, end):
            kwh = point.p50 * self._corrected_scale(point.start) * INTERVAL_HOURS
            if kwh > 0:
                h = point.start.astimezone(self.tz).hour
                remaining[h] = remaining.get(h, 0.0) + kwh
        if remaining.get(hour, 0.0) > 0:  # from the first hour with production on
            self.asof[hour] = remaining

    @property
    def fog_used(self) -> bool:
        """The fog layer may carry the rest of today (nightly choice or setting)."""
        return fog_allowed(self.settings.intraday, self.tuning)

    def _layer_b(
        self, start: datetime, horizon: datetime, state: FogState, k_hour: float | None
    ) -> float:
        """Layer B factor for the interval starting at ``start``: the last hour's ratio
        fading into the fog factor, by the lead time from ``horizon`` (end of the final
        measurements) to the middle of the part of the interval still unknown."""
        k_fog = state.k if state.active and self.fog_used else 1.0
        unknown_from = max(start, horizon)
        end = start + INTERVAL
        middle = unknown_from + (end - unknown_from) / 2 if end > unknown_from else end
        lead = (middle - horizon).total_seconds() / 3600
        return multiplier(k_hour, k_fog, lead, self.settings.intraday.decay_h)

    def _b_applied(self, start: datetime, horizon: datetime) -> float:
        if self.settings.fog.mode is not FogMode.ON:
            return 1.0
        return self._layer_b(start, horizon, self.fog, self.k_hour)

    def _b_shadow(self, start: datetime, horizon: datetime) -> float:
        return self._layer_b(start, horizon, self.fog, self.k_hour)

    def _b_weather(self, start: datetime, horizon: datetime) -> float:
        """Layer B factor the published forecast carries (or would carry, in shadow mode)
        with the weather factor applied."""
        if self.settings.weather.mode is WeatherMode.ON:
            return self._b_applied(start, horizon)  # the published layer already runs with it
        if self.settings.fog.mode is not FogMode.ON:
            return 1.0
        return self._layer_b(start, horizon, self.fog_weather, self.k_hour_weather)

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
        self,
        points: list[ForecastPoint],
        start: datetime,
        end: datetime,
        horizon: datetime,
        measured: float = 0.0,
    ) -> dict[str, Band]:
        """Base, corrected and (in shadow mode) shadow version of the base between ``start``
        and ``end``; ``measured`` is added to the corrected variants. Layer B depends on
        the lead time from ``horizon``, the end of the final measurements."""

        def corrected(at: datetime) -> float:
            return self._corrected_scale(at) * self._b_applied(at, horizon)

        bands = {
            BASE: self._base_band(points, start, end),
            CORRECTED: self._base_band(points, start, end, corrected).scaled(1.0, measured),
        }
        if self.settings.fog.mode is FogMode.SHADOW:

            def shadow(at: datetime) -> float:
                return self._corrected_scale(at) * self._b_shadow(at, horizon)

            bands[SHADOW] = self._base_band(points, start, end, shadow).scaled(1.0, measured)
        if self._weather_in_shadow:

            def with_weather(at: datetime) -> float:
                return self._weather_scale(at) * self._b_weather(at, horizon)

            bands[WEATHER] = self._base_band(points, start, end, with_weather).scaled(1.0, measured)
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
            variants = self._variants(today_base, until, end, until, measured)
            variants[BASE] = self._base_band(today_base, start, end)
            self.checkpoints.day_total = variants

        for wall in REST_CHECKPOINTS:
            key = checkpoint_key(wall)
            nominal = local_time(self.day, wall, self.tz)
            if key in self.checkpoints.rest or not nominal <= now < nominal + CAPTURE_WINDOW:
                continue
            if today_base:
                horizon = min(nominal, self.measured_until(now))
                self.checkpoints.rest[key] = self._variants(today_base, nominal, end, horizon)

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
            fog_active=self.fog.active and self.fog_used,
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
            layer_b = self._b_applied(point.start, horizon)
            corrected = point.scaled(self._corrected_scale(point.start) * layer_b)
            if interval_end <= now:
                # Past: measured where readings cover the interval, the corrected forecast
                # of record for the part not measured yet.
                record = self.record.get(point.start, point)
                weather = (
                    self.weather_factor_record.get(floor_to(point.start, HOUR), 1.0)
                    if self.settings.weather.mode is WeatherMode.ON
                    else 1.0
                )
                record = record.scaled(self.level_at(point.start) * weather * layer_b)
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
            fog=self._fog_status(now, today_base),
            report=self.report,
            base_valid=self.base_valid,
            base_refreshed_at=self.base_refreshed_at,
            base_failures_today=self.base_failures_today,
            history=tuple(self.history[d] for d in sorted(self.history)),
            weather=self._weather_status(now, today_base, tomorrow_base),
        )

    def _fog_status(self, now: datetime, today_base: list[ForecastPoint]) -> FogStatus:
        """Layer B as published, with its factor on the next hour and on the rest of today
        (applied or, in shadow mode, what it would be)."""
        horizon = self.measured_until(now)
        mode = self.settings.fog.mode

        def factor(span_end: datetime | None) -> float | None:
            if mode is FogMode.OFF:
                return None
            plain = scaled = 0.0
            for point in today_base:
                end = point.start + INTERVAL
                if end <= now or (span_end is not None and point.start >= span_end):
                    continue
                kwh = (
                    point.p50
                    * self._corrected_scale(point.start)
                    * ((end - max(now, point.start)).total_seconds() / 3600)
                )
                plain += kwh
                scaled += kwh * self._b_shadow(point.start, horizon)
            return scaled / plain if plain > 0 else None

        return FogStatus(
            mode=mode,
            gate=self.settings.fog.gate.name,
            applied=self.fog_applied,
            shadow=self.fog_shadow,
            state=self.fog,
            k_hour=self.k_hour,
            short_term=self.short_term,
            fog_used=self.fog_used,
            tuning=self.tuning,
            next_hour_factor=factor(now + HOUR),
            rest_factor=factor(None),
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
        horizon = self.measured_until(now)

        def summary(points: list[ForecastPoint], start: datetime, today: bool) -> tuple[Any, ...]:
            plain = weighted = with_weather = 0.0
            hours: dict[str, float] = {}
            for point in points:
                layer_b = self._b_weather(point.start, horizon) if today else 1.0
                end = point.start + INTERVAL
                hours_left = max(0.0, (end - max(start, point.start)).total_seconds()) / 3600
                level = self.level_at(point.start) * point.p50 * hours_left
                plain += level
                weighted += level * self.weather_at(point.start)
                with_weather += level * layer_b * self.weather_at(point.start)
                hour = floor_to(point.start, HOUR)
                if hour in self._weather_factors:
                    label = f"{local_hour(hour, self.tz):02d}:00"
                    hours[label] = round(self._weather_factors[hour], 3)
            # The weather factor alone (energy-weighted); the estimate includes layer B.
            factor = weighted / plain if plain > 0 else None
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
        r_factor, r_estimate, _, r_sun, r_fog = summary(rest, start, True)
        t_start = tomorrow_base[0].start if tomorrow_base else now
        t_factor, t_estimate, t_hours, t_sun, t_fog = summary(tomorrow_base, t_start, False)
        today_hours = summary(today_base, today_base[0].start, True)[2] if today_base else {}
        hourly: dict[datetime, list[float]] = {}
        for points, today in ((today_base, True), (tomorrow_base, False)):
            for point in points:
                kwh = point.p50 * self.level_at(point.start) * INTERVAL_HOURS
                sums = hourly.setdefault(floor_to(point.start, HOUR), [0.0, 0.0])
                plain_b = self._b_applied(point.start, horizon) if today else 1.0
                weather_b = self._b_weather(point.start, horizon) if today else 1.0
                sums[0] += kwh * plain_b
                sums[1] += kwh * weather_b * self.weather_at(point.start)
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
            "fog_weather": self.fog_weather.to_dict(),
            "weather_mode": self.settings.weather.mode.value,
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
            "intraday": {
                "k_hour": self.k_hour,
                "k_hour_weather": self.k_hour_weather,
                "short_term": self.short_term.value,
                "tuning": self.tuning.to_dict(),
                "days": [self.tuning_days[d].to_dict() for d in sorted(self.tuning_days)],
                "asof": [
                    [h, [[j, round(v, 4)] for j, v in sorted(f.items())]]
                    for h, f in sorted(self.asof.items())
                ],
            },
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
        if data.get("fog_weather") and settings.fog.mode is not FogMode.OFF:
            engine.fog_weather = FogState.from_dict(data["fog_weather"])
        previous_mode = data.get("weather_mode")
        if settings.fog.mode is not FogMode.OFF and previous_mode != settings.weather.mode.value:
            if settings.weather.mode is WeatherMode.ON and previous_mode == WeatherMode.SHADOW:
                # Switched on: the fog layer continues as it ran with the weather factor.
                engine.fog = engine.fog_weather
            elif previous_mode == WeatherMode.ON:
                # Switched off or to shadow: the weather world is the old published one; the
                # published fog layer starts again from today's measurements.
                engine.fog_weather = engine.fog
                engine.fog = FogState(reason=engine._initial_fog_reason())
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
        if "fog_weather" not in data:
            # State of 0.5.2 or earlier recorded the applied factor (1.0 in shadow mode); take
            # today's factors as they stand now instead.
            engine.weather_factor_record = {
                hour: engine.weather_at(hour) for hour in engine.weather_factor_record
            }
        intraday = data.get("intraday") or {}
        if engine.short_term is not ShortTermReason.DISABLED:
            engine.k_hour = intraday.get("k_hour")
            engine.k_hour_weather = intraday.get("k_hour_weather")
            try:
                engine.short_term = ShortTermReason(intraday.get("short_term", "waiting"))
            except ValueError:
                engine.short_term = ShortTermReason.WAITING
        engine.tuning_days = {
            t.day: t for t in (TuningDay.from_dict(item) for item in intraday.get("days", []))
        }
        engine.asof = {
            int(h): {int(j): float(v) for j, v in f} for h, f in intraday.get("asof", [])
        }
        if engine.day is not None:
            engine._tune()  # settings may have changed since the state was saved
        return engine
