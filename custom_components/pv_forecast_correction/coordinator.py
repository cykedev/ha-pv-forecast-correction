"""Feeds the correction engine from Home Assistant and publishes its result."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
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
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, PowerConverter

from .config import Inputs, inputs_from, merged, settings_dict, settings_from
from .const import (
    ARCHIVE_EVERY_MINUTES,
    CONF_ARCHIVE_ENABLED,
    CONF_ARCHIVE_RETENTION_DAYS,
    DOMAIN,
    ISSUE_AFTER_MINUTES,
    SAVE_DELAY_SECONDS,
    STORAGE_KEY,
    STORE_VERSION,
    UPDATE_MINUTES,
    UPDATE_SECOND,
    WARN_AFTER_MINUTES,
)
from .core import Corrector, Forecast, ForecastFormatError, Result, Settings, parse_forecast
from .core.archive import Archive, ArchiveError, CorrectionRow, MeasuredBin

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
        self._raised = False
        # An issue raised before a restart or reload is re-raised if the problem persists.
        ir.async_delete_issue(hass, DOMAIN, issue_id)

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
        now = dt_util.utcnow()
        if self.engine.day == dt_util.as_local(now).date():
            previous = self.engine.level
            self.engine.recompute_level(now)
            if (self.engine.level.factor, self.engine.level.days) == (
                previous.factor,
                previous.days,
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
            except ArchiveError as err:
                self.archive_error = str(err)
                _LOGGER.warning("Archive disabled, %s cannot be used: %s", archive.path, err)
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
        self._read_inputs(now, base=True)
        self._refresh(now)

    async def async_stop(self) -> None:
        """Unsubscribe, save the state and close the archive."""
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []
        await self._store.async_save(self.engine.to_dict())
        if self.archive is not None:
            await self.hass.async_add_executor_job(self.archive.close)
            self.archive = None

    async def async_remove_data(self) -> None:
        """Delete the stored state of a removed entry (the archive file is kept)."""
        await self._store.async_remove()

    # ------------------------------------------------------------------ events

    @callback
    def _on_started(self, _hass: HomeAssistant) -> None:
        now = dt_util.utcnow()
        self._read_inputs(now, base=True)
        self._refresh(now)

    @callback
    def _on_forecast_change(self, event: Event[EventStateChangedData]) -> None:
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

    def _parse(self, entity_id: str) -> tuple[Forecast | None, str | None]:
        state = self.hass.states.get(entity_id)
        if state is None:
            return None, f"{entity_id} does not exist"
        if state.state in _INVALID_STATES:
            return None, f"{entity_id} is {state.state}"
        try:
            return parse_forecast(state.attributes), None
        except ForecastFormatError as err:
            return None, f"{entity_id}: {err}"

    def _meter_source(self) -> str:
        if self.inputs.actual_energy:
            return f"energy:{self.inputs.actual_energy}"
        return "power:" + ",".join(sorted(self.inputs.actual_power))

    def _read_base(self, now: datetime) -> None:
        today, problem = self._parse(self.inputs.forecast_today)
        if today is None and not self.hass.is_running:
            # The base integration may not be loaded yet; it is read again once Home
            # Assistant has started. Keep the restored forecast meanwhile.
            return
        self._base_problem.report(now, problem, {"entity_id": self.inputs.forecast_today})
        if today is None:
            self.engine.base_unavailable(now)
            return
        forecast = today
        if self.inputs.forecast_tomorrow:
            tomorrow, tomorrow_problem = self._parse(self.inputs.forecast_tomorrow)
            if tomorrow is not None:
                forecast = today.merged_with(tomorrow)
            else:
                _LOGGER.debug("Tomorrow's forecast not used: %s", tomorrow_problem)
        update = self.engine.set_base(forecast, now)
        if update.changed and self.archive is not None:
            self._archive_job(
                "snapshot",
                self.archive.add_snapshot,
                now,
                self.engine.base,
                refreshed=update.refreshed,
            )

    def _number(
        self, entity_id: str
    ) -> tuple[float | None, str | None, datetime | None, str | None]:
        """Numeric state, its unit, when it was last reported and a problem description."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return None, None, None, f"{entity_id} does not exist"
        if state.state in _INVALID_STATES:
            return None, None, None, f"{entity_id} is {state.state}"
        try:
            value = float(state.state)
        except ValueError:
            return None, None, None, f"{entity_id} is not numeric ({state.state!r})"
        return value, state.attributes.get("unit_of_measurement"), state.last_reported, None

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
            # A meter reading is the energy at the time it was reported, not at ``now``:
            # meters that report every few minutes must not be shifted to the update tick.
            entity_id = self.inputs.actual_energy
            value, unit, reported, problem = self._number(entity_id)
            kwh = None if value is None else self._to_kwh(entity_id, value, unit)
            self.engine.observe_energy(reported or now, kwh)
        else:
            # Power holds until it changes, so the sum of the current values is valid now.
            total: float | None = 0.0
            for entity_id in self.inputs.actual_power:
                value, unit, _reported, problem = self._number(entity_id)
                if value is None:
                    total = None
                    break
                assert total is not None
                total += self._to_kw(entity_id, value, unit)
            self.engine.observe_power(now, total)
        if problem is not None and (
            not self.hass.is_running or not self.engine.expects_production(now)
        ):
            # Many inverters go unavailable at night; that is no problem worth reporting.
            problem = None
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
        previous_report = self.engine.report
        try:
            self.engine.update(now)
            result = self.engine.result(now)
        except Exception as err:
            message = f"{type(err).__name__}: {err}"
            if message != self.last_error:
                _LOGGER.exception("Correction failed, keeping the last result")
            self.last_error = message
            return
        self.last_error = None
        self._archive_cycle(now, result, previous_report)
        self.async_set_updated_data(result)
        self._store.async_delay_save(self.engine.to_dict, SAVE_DELAY_SECONDS)

    def _archive_cycle(self, now: datetime, result: Result, previous_report: Any) -> None:
        if self.archive is None:
            return
        bins = [
            MeasuredBin(start, item.energy_kwh, item.covered_s, item.curtailed)
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
                ),
            )
        report = self.engine.report
        if report is not None and report is not previous_report:
            all_bins = [
                MeasuredBin(start, item.energy_kwh, item.covered_s, item.curtailed)
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
        files = await self.hass.async_add_executor_job(self.archive.export_csv, target)
        return [str(path) for path in files]

    async def async_reset(self, *, clear_archive: bool) -> None:
        """Forget what layer A learned (and optionally the archive)."""
        self.engine.reset_learning()
        now = dt_util.utcnow()
        self.engine.recompute_level(now)
        if clear_archive and self.archive is not None:
            await self.hass.async_add_executor_job(self.archive.clear)
        await self._store.async_save(self.engine.to_dict())
        self._refresh(now)
