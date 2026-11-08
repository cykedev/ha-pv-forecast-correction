"""Config and options flow."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .config import merged
from .const import (
    CONF_ACTUAL_ENERGY,
    CONF_ACTUAL_POWER,
    CONF_ARCHIVE_ENABLED,
    CONF_ARCHIVE_RETENTION_DAYS,
    CONF_CURTAILMENT,
    CONF_FOG_CONFIDENCE,
    CONF_FOG_MIN_FACTOR,
    CONF_FOG_MODE,
    CONF_FOG_RECENT_HOURS,
    CONF_FOG_THRESHOLD,
    CONF_FORECAST_TODAY,
    CONF_FORECAST_TOMORROW,
    CONF_LEVEL_DAYS,
    CONF_LEVEL_ENABLED,
    CONF_LEVEL_MAX,
    CONF_LEVEL_MIN,
    CONF_LEVEL_SHRINK_DAYS,
    DEFAULT_NAME,
    DOMAIN,
    SECTION_ARCHIVE,
    SECTION_FOG,
    SECTION_INPUTS,
    SECTION_LEVEL,
)
from .core import FogMode, ForecastFormatError, parse_forecast


def _number(minimum: float, maximum: float, step: float, unit: str | None = None):
    config = NumberSelectorConfig(min=minimum, max=maximum, step=step, mode=NumberSelectorMode.BOX)
    if unit is not None:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


def _input_fields() -> dict[vol.Marker, Any]:
    return {
        vol.Required(CONF_FORECAST_TODAY): EntitySelector(EntitySelectorConfig(domain="sensor")),
        vol.Optional(CONF_FORECAST_TOMORROW): EntitySelector(EntitySelectorConfig(domain="sensor")),
        vol.Optional(CONF_ACTUAL_ENERGY): EntitySelector(
            EntitySelectorConfig(domain="sensor", device_class=SensorDeviceClass.ENERGY)
        ),
        vol.Optional(CONF_ACTUAL_POWER): EntitySelector(
            EntitySelectorConfig(
                domain="sensor", device_class=SensorDeviceClass.POWER, multiple=True
            )
        ),
        vol.Optional(CONF_CURTAILMENT): EntitySelector(
            EntitySelectorConfig(domain="binary_sensor")
        ),
    }


def _validate_forecast(hass: HomeAssistant, entity_id: str | None) -> str | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return "entity_not_found"
    if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None  # the base integration may still be starting
    try:
        parse_forecast(state.attributes)
    except ForecastFormatError:
        return "invalid_forecast"
    return None


def validate_inputs(hass: HomeAssistant, data: Mapping[str, Any]) -> dict[str, str]:
    """Errors keyed by field (or ``base``)."""
    errors: dict[str, str] = {}
    for key in (CONF_FORECAST_TODAY, CONF_FORECAST_TOMORROW):
        if error := _validate_forecast(hass, data.get(key)):
            errors[key] = error
    if data.get(CONF_FORECAST_TOMORROW) and data.get(CONF_FORECAST_TOMORROW) == data.get(
        CONF_FORECAST_TODAY
    ):
        errors[CONF_FORECAST_TOMORROW] = "same_forecast"
    energy = data.get(CONF_ACTUAL_ENERGY)
    power = data.get(CONF_ACTUAL_POWER) or []
    if not energy and not power:
        errors["base"] = "actual_missing"
    elif energy and power:
        errors["base"] = "actual_both"
    return errors


def _clean_inputs(data: Mapping[str, Any]) -> dict[str, Any]:
    """Explicit empty values, so a cleared optional input overrides the entry data."""
    return {
        CONF_FORECAST_TODAY: data[CONF_FORECAST_TODAY],
        CONF_FORECAST_TOMORROW: data.get(CONF_FORECAST_TOMORROW) or None,
        CONF_ACTUAL_ENERGY: data.get(CONF_ACTUAL_ENERGY) or None,
        CONF_ACTUAL_POWER: list(data.get(CONF_ACTUAL_POWER) or []),
        CONF_CURTAILMENT: data.get(CONF_CURTAILMENT) or None,
    }


class PvForecastCorrectionConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a correction for one base forecast."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate_inputs(self.hass, user_input)
            if not errors:
                self._async_abort_entries_match(
                    {CONF_FORECAST_TODAY: user_input[CONF_FORECAST_TODAY]}
                )
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME) or DEFAULT_NAME,
                    data=_clean_inputs(user_input),
                )
        schema = vol.Schema(
            {vol.Required(CONF_NAME, default=DEFAULT_NAME): TextSelector(), **_input_fields()}
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return PvForecastCorrectionOptionsFlow()


class PvForecastCorrectionOptionsFlow(OptionsFlow):
    """Change inputs and every parameter of the correction."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = merged(self.config_entry.data, self.config_entry.options)
        errors: dict[str, str] = {}
        if user_input is not None:
            flat: dict[str, Any] = {}
            for name in (SECTION_INPUTS, SECTION_LEVEL, SECTION_FOG, SECTION_ARCHIVE):
                flat.update(user_input.get(name, {}))
            errors = validate_inputs(self.hass, flat)
            if float(flat[CONF_LEVEL_MIN]) >= float(flat[CONF_LEVEL_MAX]):
                errors["base"] = "level_bounds"
            if not errors:
                flat.update(_clean_inputs(flat))
                return self.async_create_entry(data=flat)
            current = {**current, **flat}

        inputs = vol.Schema(_input_fields())
        level = vol.Schema(
            {
                vol.Required(CONF_LEVEL_ENABLED): BooleanSelector(),
                vol.Required(CONF_LEVEL_DAYS): _number(3, 60, 1, "d"),
                vol.Required(CONF_LEVEL_SHRINK_DAYS): _number(0, 14, 0.5, "d"),
                vol.Required(CONF_LEVEL_MIN): _number(0.1, 1.0, 0.05),
                vol.Required(CONF_LEVEL_MAX): _number(1.0, 3.0, 0.05),
            }
        )
        fog = vol.Schema(
            {
                vol.Required(CONF_FOG_MODE): SelectSelector(
                    SelectSelectorConfig(
                        options=[mode.value for mode in FogMode],
                        translation_key=CONF_FOG_MODE,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(CONF_FOG_THRESHOLD): _number(0.1, 1.0, 0.05),
                vol.Required(CONF_FOG_CONFIDENCE): _number(0.0, 1.0, 0.05),
                vol.Required(CONF_FOG_RECENT_HOURS): _number(0.5, 6.0, 0.5, "h"),
                vol.Required(CONF_FOG_MIN_FACTOR): _number(0.05, 1.0, 0.05),
            }
        )
        archive = vol.Schema(
            {
                vol.Required(CONF_ARCHIVE_ENABLED): BooleanSelector(),
                vol.Required(CONF_ARCHIVE_RETENTION_DAYS): _number(30, 3650, 1, "d"),
            }
        )
        sections = {
            SECTION_INPUTS: (inputs, False),
            SECTION_LEVEL: (level, True),
            SECTION_FOG: (fog, True),
            SECTION_ARCHIVE: (archive, True),
        }
        schema = vol.Schema(
            {
                vol.Required(name): section(
                    self.add_suggested_values_to_schema(
                        fields, {k: v for k, v in current.items() if v is not None}
                    ),
                    {"collapsed": collapsed},
                )
                for name, (fields, collapsed) in sections.items()
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
