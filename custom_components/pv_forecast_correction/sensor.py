"""Sensors: corrected forecasts in the Solcast format, factors and errors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import tzinfo
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import CorrectionCoordinator, PvForecastCorrectionConfigEntry
from .core import DayForecast, Result
from .core.forecast_format import detailed_forecast, detailed_hourly, rounded_energy
from .core.score import BASE, CORRECTED, SHADOW, DayReport, VariantErrors

PARALLEL_UPDATES = 0

FORECAST_LISTS = frozenset({"detailedForecast", "detailedHourly"})


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _forecast_attributes(
    forecast: DayForecast | None, tz: tzinfo, *, hourly: bool
) -> dict[str, Any]:
    if forecast is None:
        return {}
    attributes: dict[str, Any] = {
        "estimate": rounded_energy(forecast.estimate),
        "estimate10": rounded_energy(forecast.estimate10),
        "estimate90": rounded_energy(forecast.estimate90),
        "base_estimate": rounded_energy(forecast.base_estimate),
        "base_estimate10": rounded_energy(forecast.base_estimate10),
        "base_estimate90": rounded_energy(forecast.base_estimate90),
        "detailedForecast": detailed_forecast(forecast.points, tz),
    }
    if hourly:
        attributes["detailedHourly"] = detailed_hourly(forecast.points, tz)
    return {key: value for key, value in attributes.items() if value is not None}


def _today_attributes(result: Result, tz: tzinfo) -> dict[str, Any]:
    attributes = _forecast_attributes(result.today, tz, hourly=True)
    if result.today is not None:
        attributes["actual_so_far"] = _round(result.actual_so_far)
        attributes["level_factor"] = _round(result.level_applied)
        attributes["fog_factor"] = _round(result.fog.applied)
    return attributes


def _remaining_attributes(result: Result, tz: tzinfo) -> dict[str, Any]:
    return _forecast_attributes(result.remaining, tz, hourly=False)


def _tomorrow_attributes(result: Result, tz: tzinfo) -> dict[str, Any]:
    attributes = _forecast_attributes(result.tomorrow, tz, hourly=True)
    if result.tomorrow is not None:
        attributes["level_factor"] = _round(result.level_applied)
    return attributes


def _level_status(result: Result, settings_days: int, enabled: bool) -> str:
    level = result.level
    if not enabled:
        return "disabled"
    if level.days_used == 0:
        return "no_history"
    if level.clamped:
        return "clamped"
    if level.days_used < settings_days:
        return "learning"
    return "learned"


def _daily_ratios(result: Result) -> dict[str, float | None]:
    """Measured ÷ forecast for exactly the days the level factor uses."""
    by_day = {stats.day: stats for stats in result.history}
    return {
        day.isoformat(): _round(by_day[day].ratio) for day in result.level.days if day in by_day
    }


def _error_attributes(report: DayReport | None, variant: str) -> dict[str, Any]:
    if report is None:
        return {"status": "no_report"}
    errors = report.variants.get(variant)
    attributes: dict[str, Any] = {
        "status": "ok" if report.complete and errors is not None else "incomplete",
        "date": report.day.isoformat(),
        "actual": _round(report.actual_kwh),
    }
    if errors is None:
        return attributes
    attributes |= _variant_attributes(errors, "")
    if variant == CORRECTED:
        attributes["level_factor"] = _round(report.level_factor)
        attributes["fog_active"] = report.fog_active
        shadow = report.variants.get(SHADOW)
        if shadow is not None:
            attributes |= {
                key: value
                for key, value in _variant_attributes(shadow, "shadow_").items()
                if value is not None
            }
    return attributes


def _variant_attributes(errors: VariantErrors, prefix: str) -> dict[str, Any]:
    return {
        f"{prefix}rest_of_day_mae": _round(errors.rest_mae),
        f"{prefix}rest_of_day": {key: _round(value) for key, value in errors.rest.items()},
        f"{prefix}day_error": _round(errors.day),
        f"{prefix}day_ahead_error": _round(errors.day_ahead),
        f"{prefix}day_p10_held": errors.day_p10_held,
        f"{prefix}day_p90_held": errors.day_p90_held,
    }


def _error_value(report: DayReport | None, variant: str) -> float | None:
    if report is None or not report.complete:
        return None
    errors = report.variants.get(variant)
    return None if errors is None else _round(errors.rest_mae)


@dataclass(frozen=True, kw_only=True)
class CorrectionSensorDescription(SensorEntityDescription):
    """Describes one sensor of the integration."""

    value: Callable[[CorrectionCoordinator, Result], Any]
    attributes: Callable[[CorrectionCoordinator, Result, tzinfo], dict[str, Any]]


def _energy_sensor(
    key: str,
    forecast: Callable[[Result], DayForecast | None],
    attributes: Callable[[Result, tzinfo], dict[str, Any]],
) -> CorrectionSensorDescription:
    return CorrectionSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda _c, r: None if forecast(r) is None else _round(forecast(r).estimate),
        attributes=lambda _c, r, tz: attributes(r, tz),
    )


SENSORS: tuple[CorrectionSensorDescription, ...] = (
    _energy_sensor("today", lambda r: r.today, _today_attributes),
    _energy_sensor("remaining_today", lambda r: r.remaining, _remaining_attributes),
    _energy_sensor("tomorrow", lambda r: r.tomorrow, _tomorrow_attributes),
    CorrectionSensorDescription(
        key="level_factor",
        translation_key="level_factor",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value=lambda _c, r: _round(r.level.factor),
        attributes=lambda c, r, _tz: {
            "status": _level_status(r, c.settings.level.days, c.settings.level_enabled),
            "applied": c.settings.level_enabled,
            "days_used": r.level.days_used,
            "days_wanted": c.settings.level.days,
            "sum_actual": _round(r.level.sum_actual),
            "sum_forecast": _round(r.level.sum_forecast),
            "raw_ratio": _round(r.level.raw_ratio),
            "shrink_weight": _round(r.level.shrink_weight),
            "clamped": r.level.clamped,
            "computed_at": r.level.computed_at,
            "daily_ratios": _daily_ratios(r),
        },
    ),
    CorrectionSensorDescription(
        key="fog_factor",
        translation_key="fog_factor",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value=lambda _c, r: _round(r.fog.applied),
        attributes=lambda _c, r, _tz: {
            "mode": r.fog.mode.value,
            "reason": r.fog.state.reason.value,
            "gate": r.fog.gate,
            "k_shadow": _round(r.fog.shadow),
            "k_morning": _round(r.fog.state.k_morning),
            "k_recent": _round(r.fog.state.k_recent),
            "base_confidence": _round(r.fog.state.confidence),
            "active_since": r.fog.state.active_since,
        },
    ),
    CorrectionSensorDescription(
        key="error_base",
        translation_key="error_base",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda _c, r: _error_value(r.report, BASE),
        attributes=lambda _c, r, _tz: _error_attributes(r.report, BASE),
    ),
    CorrectionSensorDescription(
        key="error_corrected",
        translation_key="error_corrected",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda _c, r: _error_value(r.report, CORRECTED),
        attributes=lambda _c, r, _tz: _error_attributes(r.report, CORRECTED),
    ),
    CorrectionSensorDescription(
        key="base_age",
        translation_key="base_age",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda _c, r: r.base_refreshed_at,
        attributes=lambda c, r, _tz: {
            "status": "ok" if r.base_valid else "unavailable",
            "failures_today": r.base_failures_today,
            "forecast_entities": c.inputs.forecast_entities,
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PvForecastCorrectionConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the sensors of one config entry."""
    coordinator = entry.runtime_data
    async_add_entities(CorrectionSensor(coordinator, description) for description in SENSORS)


class CorrectionSensor(CoordinatorEntity[CorrectionCoordinator], SensorEntity):
    """A sensor that reads one part of the engine result."""

    entity_description: CorrectionSensorDescription
    _attr_has_entity_name = True
    # The interval lists are large and change often: keep them out of the recorder.
    _unrecorded_attributes = FORECAST_LISTS

    def __init__(
        self, coordinator: CorrectionCoordinator, description: CorrectionSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
            model="Forecast correction",
        )

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value(self.coordinator, self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        return self.entity_description.attributes(
            self.coordinator, self.coordinator.data, dt_util.get_default_time_zone()
        )
