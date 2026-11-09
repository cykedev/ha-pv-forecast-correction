"""Feeds the correction engine from Home Assistant and publishes its result."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, PowerConverter

from .config import Inputs, inputs_from, merged, settings_dict, settings_from
from .const import (
    ARCHIVE_EVERY_MINUTES,
    CONF_ARCHIVE_ENABLED,
    CONF_ARCHIVE_RETENTION_DAYS,
    DOMAIN,
    FORECAST_SETTLE_SECONDS,
    ISSUE_AFTER_MINUTES,
    SAVE_DELAY_SECONDS,
    STORAGE_KEY,
    STORE_VERSION,
    UPDATE_MINUTES,
    UPDATE_SECOND,
    WARN_AFTER_MINUTES,
    WEATHER_FETCH_MINUTE,
)
from .core import Corrector, Forecast, ForecastFormatError, Result, Settings, parse_forecast
from .core.archive import Archive, ArchiveError, CorrectionRow, MeasuredBin
from .core.history import HOUR, days_from_hourly
from .core.timeline import day_bounds, local_day, steps
from .core.weather import Station, WeatherError, WeatherMode
from .weather_source import async_fetch_mosmix, async_nearest_station, async_station

_LOGGER = logging.getLogger(__name__)

_INVALID_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN, None)

type PvForecastCorrectionConfigEntry = ConfigEntry[CorrectionCoordinator]


def issue_id(kind: str, entry: ConfigEntry) -> str:
    """Repair issue id of an input problem of one entry."""
    return f"{kind}_{entry.entry_id}"


class _Problem:
    """Tracks one input problem: logs on change, raises a repair issue if it persists."""

    def __init__(self, hass: HomeAssistant, issue_id: str, translation_key: str) -> None:
        self._hass = hass
        self._issue_id = issue_id
        self._key = translation_key
        self.since: datetime | None = None
        self.detail: str | None = None
        self._logged = False
        # An issue raised before a restart or reload stays until the input works again.
        self._raised = ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    def hold(self) -> None:
        """The input cannot be judged now (no production expected): a raised issue stays,
        a problem that has not become one yet starts counting anew next time."""
        if not self._raised:
            self.since = None

    def clear(self) -> None:
        """Remove the issue (the entry is disabled or removed)."""
        ir.async_delete_issue(self._hass, DOMAIN, self._issue_id)
        self._raised = False

    def report(self, now: datetime, detail: str | None, placeholders: dict[str, str]) -> None:
        """Report the current problem (``None`` when everything is fine)."""
        if detail is None:
            if self._logged:
                _LOGGER.info("Input problem resolved: %s", self.detail)
            self.since = self.detail = None
            self._logged = False
            if self._raised:
                ir.async_delete_issue(self._hass, DOMAIN, self._issue_id)
                self._raised = False
            return
        if detail != self.detail:
            _LOGGER.debug("Input problem: %s", detail)
        if self.since is None:
            self.since = now
        self.detail = detail
        if not self._logged and now - self.since >= timedelta(minutes=WARN_AFTER_MINUTES):
            _LOGGER.warning("Input problem for %s: %s", now - self.since, detail)
            self._logged = True
        if not self._raised and now - self.since >= timedelta(minutes=ISSUE_AFTER_MINUTES):
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=self._key,
                translation_placeholders={**placeholders, "detail": detail},
            )
            self._raised = True


class CorrectionCoordinator(DataUpdateCoordinator[Result]):
    """One instance per config entry."""

    config_entry: PvForecastCorrectionConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PvForecastCorrectionConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=entry.title, update_interval=None)
        config = merged(entry.data, entry.options)
        self.inputs: Inputs = inputs_from(config)
        self.settings: Settings = settings_from(config)
        self.archive_enabled = bool(config[CONF_ARCHIVE_ENABLED])
        self.retention = timedelta(days=int(config[CONF_ARCHIVE_RETENTION_DAYS]))
        self.engine = Corrector(self.settings, dt_util.get_default_time_zone())
        self.archive: Archive | None = None
        self.archive_error: str | None = None
        self.last_error: str | None = None
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
        )
        self._unsubscribe: list[CALLBACK_TYPE] = []
        self._units_warned: set[str] = set()
        self._base_problem = _Problem(hass, issue_id("forecast", entry), "forecast_unavailable")
        self._actual_problem = _Problem(hass, issue_id("actual", entry), "actual_unavailable")
        self._archived_report: Any = None
        self._pending_base: CALLBACK_TYPE | None = None
        self.weather_station: Station | None = None
        self.weather_error: str | None = None
        self._weather_modified: str | None = None

    @property
    def archive_path(self) -> Path:
        return Path(self.hass.config.path(DOMAIN, f"{self.config_entry.entry_id}.db"))

    # ------------------------------------------------------------------ lifecycle

    async def async_start(self) -> None:
        """Restore state, open the archive, subscribe to inputs and run once."""
        tz = dt_util.get_default_time_zone()
        stored = await self._store.async_load()
        if stored:
            try:
                self.engine = Corrector.from_dict(stored, self.settings, tz)
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Stored state could not be restored, starting fresh: %s", err)
                self.engine = Corrector(self.settings, tz)
        # Readings of a different measurement input must not be continued (spikes).
        self.engine.use_meter_source(self._meter_source())
        self._archived_report = self.engine.report  # archived before the restart
        now = dt_util.utcnow()
        if self.engine.day == dt_util.as_local(now).date():
            previous = self.engine.level
            self.engine.recompute_level(now)
            if (self.engine.level.factor, self.engine.level.days, self.engine.level.curve) == (
                previous.factor,
                previous.days,
                previous.curve,
            ):
                self.engine.level = previous  # unchanged: keep the nightly timestamp

        if self.archive_enabled:
            archive = Archive(self.archive_path)
            try:
                await self.hass.async_add_executor_job(archive.open)
                await self.hass.async_add_executor_job(
                    archive.set_meta, "time_zone", self.hass.config.time_zone
                )
                await self.hass.async_add_executor_job(
                    archive.set_meta, "settings", json.dumps(settings_dict(self.settings))
                )
            except (ArchiveError, OSError, sqlite3.Error) as err:
                # The correction works without its archive.
                self.archive_error = str(err)
                _LOGGER.warning("Archive disabled, %s cannot be used: %s", archive.path, err)
                await self.hass.async_add_executor_job(archive.close)
            else:
                self.archive = archive

        self._unsubscribe = [
            async_track_state_change_event(
                self.hass, self.inputs.forecast_entities, self._on_forecast_change
            ),
            async_track_state_change_event(
                self.hass, self.inputs.actual_entities, self._on_actual_change
            ),
            async_track_time_change(
                self.hass, self._on_tick, minute=UPDATE_MINUTES, second=UPDATE_SECOND
            ),
        ]
        if self.inputs.curtailment:
            self._unsubscribe.append(
                async_track_state_change_event(
                    self.hass, [self.inputs.curtailment], self._on_curtailment_change
                )
            )
        self._unsubscribe.append(async_at_started(self.hass, self._on_started))
        if self.settings.weather.mode is not WeatherMode.OFF:
            self._unsubscribe.append(
                async_track_time_change(
                    self.hass, self._on_weather_tick, minute=WEATHER_FETCH_MINUTE, second=30
                )
            )
            self._on_weather_tick(dt_util.utcnow())
        now = dt_util.utcnow()
        self._read_inputs(now, base=True)
        self._refresh(now)

    async def async_stop(self) -> None:
        """Unsubscribe, save the state and close the archive (safe to call twice)."""
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []
        if self._pending_base is not None:
            self._pending_base()
            self._pending_base = None
        await self._store.async_save(self.engine.to_dict())
        if self.archive is not None:
            await self.hass.async_add_executor_job(self.archive.close)
            self.archive = None

    def clear_issues(self) -> None:
        """Remove the repair issues of this entry."""
        self._base_problem.clear()
        self._actual_problem.clear()

    async def _async_update_data(self) -> Result:
        """A requested refresh (e.g. the update_entity action) runs a full cycle."""
        now = dt_util.utcnow()
        self._read_inputs(now, base=True)
        result = self._cycle(now)
        if result is None:
            raise UpdateFailed(self.last_error or "no result")
        return result

    # ------------------------------------------------------------------ events

    @callback
    def _on_started(self, _hass: HomeAssistant) -> None:
        now = dt_util.utcnow()
        self._read_inputs(now, base=True)
        self._refresh(now)

    @callback
    def _on_weather_tick(self, _fired_at: datetime) -> None:
        self.config_entry.async_create_background_task(
            self.hass, self.async_update_weather(), f"{DOMAIN} weather"
        )

    async def async_update_weather(self) -> None:
        """Fetch the latest MOSMIX run of the station and use it if it is new."""
        wanted = self.settings.weather.station
        try:
            if self.weather_station is None or (wanted and self.weather_station.id != wanted):
                if wanted:
                    found = await async_station(self.hass, wanted)
                    self.weather_station = found or Station(wanted, "", 0.0, 0.0)
                else:
                    self.weather_station = await async_nearest_station(self.hass)
            fetched = await async_fetch_mosmix(
                self.hass, self.weather_station.id, self._weather_modified
            )
        except WeatherError as err:
            if str(err) != self.weather_error:
                _LOGGER.warning("Weather forecast not available: %s", err)
            self.weather_error = str(err)
            self.engine.weather_unavailable()
            self._refresh(dt_util.utcnow())
            return
        if self.weather_error is not None:
            _LOGGER.info("Weather forecast available again")
        self.weather_error = None
        self._weather_modified = fetched.last_modified
        if fetched.forecast is None:
            return  # not changed since the last download
        now = dt_util.utcnow()
        new = self.engine.set_weather(fetched.forecast, now)
        if new and self.archive is not None:
            self._archive_job("weather", self.archive.add_weather, now, fetched.forecast)
        self._refresh(now)

    @callback
    def _on_forecast_change(self, event: Event[EventStateChangedData]) -> None:
        if self._pending_base is not None:
            self._pending_base()
        self._pending_base = async_call_later(
            self.hass, FORECAST_SETTLE_SECONDS, self._on_forecast_settled
        )

    @callback
    def _on_forecast_settled(self, _fired_at: datetime) -> None:
        self._pending_base = None
        now = dt_util.utcnow()
        self._read_base(now)
        self._refresh(now)

    @callback
    def _on_actual_change(self, event: Event[EventStateChangedData]) -> None:
        self._read_actual(dt_util.utcnow())

    @callback
    def _on_curtailment_change(self, event: Event[EventStateChangedData]) -> None:
        self._read_curtailment(dt_util.utcnow())

    @callback
    def _on_tick(self, _fired_at: datetime) -> None:
        # One clock for every input: the time handed to timer callbacks can differ slightly
        # from utcnow(), which would put readings out of order.
        now = dt_util.utcnow()
        self._read_inputs(now, base=True)
        self._refresh(now)

    # ------------------------------------------------------------------ inputs

    def _read_inputs(self, now: datetime, *, base: bool) -> None:
        if base:
            self._read_base(now)
        self._read_actual(now)
        if self.inputs.curtailment:
            self._read_curtailment(now)

    def _parse(self, entity_id: str) -> tuple[Forecast | None, datetime | None, str | None]:
        """The forecast of an entity, when Home Assistant received it, and a problem."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return None, None, f"{entity_id} does not exist"
        if state.state in _INVALID_STATES:
            return None, None, f"{entity_id} is {state.state}"
        try:
            return parse_forecast(state.attributes), state.last_updated, None
        except ForecastFormatError as err:
            return None, None, f"{entity_id}: {err}"

    def _meter_source(self) -> str:
        if self.inputs.actual_energy:
            return f"energy:{self.inputs.actual_energy}"
        return "power:" + ",".join(sorted(self.inputs.actual_power))

    def _read_base(self, now: datetime) -> None:
        today, received, problem = self._parse(self.inputs.forecast_today)
        if today is None and not self.hass.is_running:
            # The base integration may not be loaded yet; it is read again once Home
            # Assistant has started. Keep the restored forecast meanwhile.
            return
        if today is None:
            self._base_problem.report(now, problem, {"entity_id": self.inputs.forecast_today})
            self.engine.base_unavailable(now)
            return
        forecast = today
        tomorrow_problem = None
        if self.inputs.forecast_tomorrow:
            tomorrow, received_tomorrow, tomorrow_problem = self._parse(
                self.inputs.forecast_tomorrow
            )
            if tomorrow is not None:
                forecast = today.merged_with(tomorrow)
                received = max(filter(None, (received, received_tomorrow)), default=None)
            else:
                # Keep tomorrow's last forecast rather than dropping it.
                tz = dt_util.get_default_time_zone()
                start, end = day_bounds(local_day(now, tz) + timedelta(days=1), tz)
                kept = self.engine.base.between(start, end)
                if kept:
                    forecast = today.merged_with(Forecast.from_points(kept))
        if tomorrow_problem is not None and self.hass.is_running:
            self._base_problem.report(
                now, tomorrow_problem, {"entity_id": str(self.inputs.forecast_tomorrow)}
            )
        else:
            self._base_problem.report(now, None, {"entity_id": self.inputs.forecast_today})
        # The time Home Assistant received the forecast, not the time it was read here: right
        # after a restart or installation this is when the base integration last delivered.
        received = now if received is None else min(received, now)
        update = self.engine.set_base(forecast, received)
        if update.changed and self.archive is not None:
            self._archive_job(
                "snapshot",
                self.archive.add_snapshot,
                received,
                self.engine.base,
                refreshed=update.refreshed,
            )

    def _number(
        self, entity_id: str
    ) -> tuple[float | None, str | None, datetime | None, str | None]:
        """Numeric state, its unit, when the value last changed and a problem description."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return None, None, None, f"{entity_id} does not exist"
        if state.state in _INVALID_STATES:
            return None, None, None, f"{entity_id} is {state.state}"
        try:
            value = float(state.state)
        except ValueError:
            value = math.nan
        if not math.isfinite(value):
            return None, None, None, f"{entity_id} is not numeric ({state.state!r})"
        return value, state.attributes.get("unit_of_measurement"), state.last_changed, None

    def _warn_unit(self, entity_id: str, unit: str | None, assumed: str) -> None:
        if entity_id not in self._units_warned:
            self._units_warned.add(entity_id)
            _LOGGER.warning("%s has unit %r, assuming %s", entity_id, unit, assumed)

    def _to_kwh(self, entity_id: str, value: float, unit: str | None) -> float:
        if unit in EnergyConverter.VALID_UNITS:
            return EnergyConverter.convert(value, unit, UnitOfEnergy.KILO_WATT_HOUR)
        self._warn_unit(entity_id, unit, UnitOfEnergy.KILO_WATT_HOUR)
        return value

    def _to_kw(self, entity_id: str, value: float, unit: str | None) -> float:
        if unit in PowerConverter.VALID_UNITS:
            return PowerConverter.convert(value, unit, UnitOfPower.KILO_WATT)
        self._warn_unit(entity_id, unit, UnitOfPower.WATT)
        return value / 1000

    def _read_actual(self, now: datetime) -> None:
        problem = None
        if self.inputs.actual_energy:
            # A meter value took effect when it changed; reading it again now confirms that the
            # counter has not moved since (no production), which keeps quiet evenings and
            # nights measured instead of turning them into gaps.
            entity_id = self.inputs.actual_energy
            value, unit, changed, problem = self._number(entity_id)
            kwh = None if value is None else self._to_kwh(entity_id, value, unit)
            self.engine.observe_energy(changed or now, kwh, seen=now)
        else:
            # Power holds until it changes, so the sum of the current values is valid now.
            total: float | None = 0.0
            for entity_id in self.inputs.actual_power:
                value, unit, _changed, problem = self._number(entity_id)
                if value is None:
                    total = None
                    break
                assert total is not None
                total += self._to_kw(entity_id, value, unit)
            self.engine.observe_power(now, total)
        if problem is not None and (
            not self.hass.is_running or not self.engine.expects_production(now)
        ):
            # Many inverters go unavailable at night; that is no problem worth reporting,
            # but an issue raised during the day stays until the sensor works again.
            self._actual_problem.hold()
            return
        self._actual_problem.report(
            now, problem, {"entity_id": ", ".join(self.inputs.actual_entities)}
        )

    def _read_curtailment(self, now: datetime) -> None:
        assert self.inputs.curtailment is not None
        state = self.hass.states.get(self.inputs.curtailment)
        value = None
        if state is not None and state.state in (STATE_ON, STATE_OFF):
            value = state.state == STATE_ON
        self.engine.observe_curtailment(now, value)

    # ------------------------------------------------------------------ cycle

    def _refresh(self, now: datetime) -> None:
        result = self._cycle(now)
        if result is not None:
            self.async_set_updated_data(result)

    def _cycle(self, now: datetime) -> Result | None:
        """Advance the engine, archive and schedule a save; ``None`` if it failed."""
        try:
            self.engine.update(now)
            result = self.engine.result(now)
        except Exception as err:
            message = f"{type(err).__name__}: {err}"
            if message != self.last_error:
                _LOGGER.exception("Correction failed, keeping the last result")
            self.last_error = message
            return None
        self.last_error = None
        self._archive_cycle(now, result)
        self._store.async_delay_save(self.engine.to_dict, SAVE_DELAY_SECONDS)
        return result

    def _archive_cycle(self, now: datetime, result: Result) -> None:
        if self.archive is None:
            return
        bins = [
            MeasuredBin(start, item.energy_kwh, item.covered_s, item.curtailed, item.gap_s)
            for start, item in self.engine.bins.items()
            if now - timedelta(hours=1) <= start <= now - timedelta(minutes=5)
        ]
        if bins:
            self._archive_job("bins", self.archive.add_bins, bins)
        local = dt_util.as_local(now)
        if local.minute % ARCHIVE_EVERY_MINUTES == 0:
            fog = result.fog
            self._archive_job(
                "correction",
                self.archive.add_correction,
                CorrectionRow(
                    ts=now.replace(second=0, microsecond=0),
                    level_factor=result.level_applied,
                    fog_applied=fog.applied,
                    fog_shadow=fog.shadow,
                    k_morning=fog.state.k_morning,
                    k_recent=fog.state.k_recent,
                    fog_reason=fog.state.reason.value,
                    today_kwh=None if result.today is None else result.today.estimate,
                    remaining_kwh=None if result.remaining is None else result.remaining.estimate,
                    tomorrow_kwh=None if result.tomorrow is None else result.tomorrow.estimate,
                    k_hour=fog.k_hour,
                    rest_factor=fog.rest_factor,
                    fog_used=fog.fog_used,
                ),
            )
        report = self.engine.report
        if report is not None and report is not self._archived_report:
            self._archived_report = report
            all_bins = [
                MeasuredBin(start, item.energy_kwh, item.covered_s, item.curtailed, item.gap_s)
                for start, item in self.engine.bins.items()
            ]
            self._archive_job("bins", self.archive.add_bins, all_bins)
            self._archive_job(
                "day", self.archive.add_day, report, self.engine.history.get(report.day)
            )
            self._archive_job("prune", self.archive.prune, now - self.retention)

    def _archive_job(self, what: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        async def run() -> None:
            try:
                await self.hass.async_add_executor_job(lambda: func(*args, **kwargs))
            except (ArchiveError, OSError, ValueError) as err:
                message = f"{what}: {err}"
                if message != self.archive_error:
                    _LOGGER.warning("Archive write failed (%s)", message)
                self.archive_error = message
            except Exception as err:  # sqlite3.Error and anything unexpected
                message = f"{what}: {type(err).__name__}: {err}"
                if message != self.archive_error:
                    _LOGGER.warning("Archive write failed (%s)", message)
                self.archive_error = message
            else:
                self.archive_error = None

        self.config_entry.async_create_background_task(self.hass, run(), f"{DOMAIN} archive {what}")

    # ------------------------------------------------------------------ services

    async def async_export(self) -> list[str]:
        """Write the archive as CSV files; returns their paths."""
        if self.archive is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="archive_disabled")
        stamp = dt_util.now().strftime("%Y%m%d-%H%M%S")
        target = Path(
            self.hass.config.path(DOMAIN, "export", f"{self.config_entry.entry_id}-{stamp}")
        )
        try:
            files = await self.hass.async_add_executor_job(self.archive.export_csv, target)
        except (ArchiveError, OSError, sqlite3.Error) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="archive_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        return [str(path) for path in files]

    async def async_learn_from_history(
        self, days: int, forecast_power: str | None = None
    ) -> dict[str, Any]:
        """Learn the level factor from the recorder's history of the last ``days`` days.

        Uses the long-term statistics of the production inputs and, per hour, of
        ``forecast_power`` (a sensor with the base forecast's power for the current moment)
        when given; otherwise the forecast's day total (statistics, else its state history)
        as it stood before production began. Days measured by the integration itself are
        kept; days imported earlier are replaced.
        """
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.history import (
            state_changes_during_period,
        )
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        tz = dt_util.get_default_time_zone()
        now = dt_util.utcnow()
        today = dt_util.as_local(now).date()
        first_day = today - timedelta(days=days)
        last_day = today - timedelta(days=1)
        start, _ = day_bounds(first_day, tz)
        _, end = day_bounds(last_day, tz)
        start -= HOUR  # statistics are per UTC hour, local days may start at half past
        recorder = get_instance(self.hass)
        forecast_id = self.inputs.forecast_today
        ids = {*self.inputs.actual_entities, forecast_id}
        if forecast_power:
            ids.add(forecast_power)
        stats = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            ids,
            "hour",
            {"energy": UnitOfEnergy.KILO_WATT_HOUR, "power": UnitOfPower.KILO_WATT},
            {"change", "mean", "state"},
        )

        def hourly(entity_id: str, key: str) -> dict[datetime, float]:
            return {
                datetime.fromtimestamp(row["start"], UTC): float(row[key])
                for row in stats.get(entity_id, [])
                if row.get(key) is not None
            }

        if self.inputs.actual_energy:
            energy = hourly(self.inputs.actual_energy, "change")
        else:
            series = [hourly(entity_id, "mean") for entity_id in self.inputs.actual_power]
            shared = set.intersection(*(set(item) for item in series)) if series else set()
            energy = {hour: sum(item[hour] for item in series) for hour in shared}

        forecast = hourly(forecast_id, "state") or hourly(forecast_id, "mean")
        if not forecast:
            states = await recorder.async_add_executor_job(
                partial(
                    state_changes_during_period,
                    self.hass,
                    start,
                    end,
                    forecast_id,
                    no_attributes=True,
                    include_start_time_state=True,
                )
            )
            forecast = _hourly_state_totals(states.get(forecast_id, []), start, end)

        # Mean power over an hour in kW equals the energy of that hour in kWh.
        hourly_forecast = hourly(forecast_power, "mean") if forecast_power else None
        result = days_from_hourly(
            energy, forecast, tz, first_day, last_day, hourly_forecast=hourly_forecast
        )
        usable = [day for day in result if day.usable]
        taken = self.engine.import_days(
            ((d.day, d.actual_kwh or 0.0, d.forecast_kwh or 0.0, d.hours) for d in usable), now
        )
        await self._store.async_save(self.engine.to_dict())
        self._refresh(dt_util.utcnow())
        learned = self.engine.history
        return {
            "days": [
                {
                    "date": day.day.isoformat(),
                    "actual_kwh": None if day.actual_kwh is None else round(day.actual_kwh, 3),
                    "forecast_kwh": None
                    if day.forecast_kwh is None
                    else round(day.forecast_kwh, 3),
                    "forecast_from": "hourly" if day.hours else "day_total",
                    "forecast_as_of": None
                    if day.forecast_as_of is None
                    else day.forecast_as_of.isoformat(),
                    "result": day.skipped.value
                    if day.skipped is not None
                    else "imported"
                    if learned.get(day.day) is not None and learned[day.day].imported
                    else "kept_measured",
                }
                for day in result
            ],
            "imported": taken,
            "level_factor": round(self.engine.level.factor, 4),
            "days_used": self.engine.level.days_used,
            "curve": {f"{hour:02d}:00": round(value, 3) for hour, value in self.engine.level.curve},
        }

    async def async_reset(self, *, clear_archive: bool) -> None:
        """Forget what layer A learned (and optionally the archive)."""
        self.engine.reset_learning()
        self.engine.recompute_level(dt_util.utcnow())
        if clear_archive and self.archive is not None:
            try:
                await self.hass.async_add_executor_job(self.archive.clear)
            except (ArchiveError, OSError, sqlite3.Error) as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="archive_failed",
                    translation_placeholders={"error": str(err)},
                ) from err
        await self._store.async_save(self.engine.to_dict())
        self._refresh(dt_util.utcnow())


def _hourly_state_totals(
    states: Sequence[Any], start: datetime, end: datetime
) -> dict[datetime, float]:
    """Value of a state history as it stood at the end of each hour."""
    values: list[tuple[datetime, float]] = []
    for state in states:
        try:
            values.append((state.last_changed, float(state.state)))
        except (TypeError, ValueError):
            continue
    result: dict[datetime, float] = {}
    index = 0
    current: float | None = None
    for hour in steps(start, end, HOUR):
        while index < len(values) and values[index][0] < hour + HOUR:
            current = values[index][1]
            index += 1
        if current is not None:
            result[hour] = current
    return result
