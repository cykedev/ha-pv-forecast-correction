"""Config flow and options flow."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast_correction.const import (
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
    CONF_LEVEL_BY_TIME,
    CONF_LEVEL_DAYS,
    CONF_LEVEL_ENABLED,
    CONF_LEVEL_MAX,
    CONF_LEVEL_MIN,
    CONF_LEVEL_SHRINK_DAYS,
    CONF_WEATHER_MODE,
    CONF_WEATHER_STATION,
    CONF_WEATHER_TRUST,
    DOMAIN,
)

from .conftest import (
    ENERGY_ENTITY,
    POWER_ENTITY,
    TODAY_ENTITY,
    TOMORROW_ENTITY,
    set_forecasts,
)

DAY = date(2026, 6, 1)


@pytest.fixture
def entities(hass: HomeAssistant) -> None:
    set_forecasts(hass, DAY)
    hass.states.async_set(ENERGY_ENTITY, "1.0", {"unit_of_measurement": "kWh"})
    hass.states.async_set(POWER_ENTITY, "500", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.not_a_forecast", "3", {})


async def start(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return result


@pytest.mark.usefixtures("entities")
async def test_creates_an_entry(hass: HomeAssistant) -> None:
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Roof",
            CONF_FORECAST_TODAY: TODAY_ENTITY,
            CONF_FORECAST_TOMORROW: TOMORROW_ENTITY,
            CONF_ACTUAL_ENERGY: ENERGY_ENTITY,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Roof"
    assert result["data"] == {
        CONF_FORECAST_TODAY: TODAY_ENTITY,
        CONF_FORECAST_TOMORROW: TOMORROW_ENTITY,
        CONF_ACTUAL_ENERGY: ENERGY_ENTITY,
        CONF_ACTUAL_POWER: [],
        CONF_CURTAILMENT: None,
    }


@pytest.mark.usefixtures("entities")
@pytest.mark.parametrize(
    ("data", "errors"),
    [
        (
            {CONF_FORECAST_TODAY: "sensor.not_a_forecast", CONF_ACTUAL_ENERGY: ENERGY_ENTITY},
            {CONF_FORECAST_TODAY: "invalid_forecast"},
        ),
        (
            {CONF_FORECAST_TODAY: "sensor.missing", CONF_ACTUAL_ENERGY: ENERGY_ENTITY},
            {CONF_FORECAST_TODAY: "entity_not_found"},
        ),
        ({CONF_FORECAST_TODAY: TODAY_ENTITY}, {"base": "actual_missing"}),
        (
            {
                CONF_FORECAST_TODAY: TODAY_ENTITY,
                CONF_ACTUAL_ENERGY: ENERGY_ENTITY,
                CONF_ACTUAL_POWER: [POWER_ENTITY],
            },
            {"base": "actual_both"},
        ),
        (
            {
                CONF_FORECAST_TODAY: TODAY_ENTITY,
                CONF_FORECAST_TOMORROW: TODAY_ENTITY,
                CONF_ACTUAL_ENERGY: ENERGY_ENTITY,
            },
            {CONF_FORECAST_TOMORROW: "same_forecast"},
        ),
    ],
)
async def test_rejects_invalid_input(
    hass: HomeAssistant, data: dict[str, Any], errors: dict[str, str]
) -> None:
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors


@pytest.mark.usefixtures("entities")
async def test_same_forecast_twice_is_aborted(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_FORECAST_TODAY: TODAY_ENTITY, CONF_ACTUAL_POWER: [POWER_ENTITY]},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


def options_input(**changes: Any) -> dict[str, Any]:
    sections = {
        "inputs": {
            CONF_FORECAST_TODAY: TODAY_ENTITY,
            CONF_ACTUAL_POWER: [POWER_ENTITY],
        },
        "level": {
            CONF_LEVEL_ENABLED: True,
            CONF_LEVEL_BY_TIME: False,
            CONF_LEVEL_DAYS: 10,
            CONF_LEVEL_SHRINK_DAYS: 1.0,
            CONF_LEVEL_MIN: 0.4,
            CONF_LEVEL_MAX: 1.2,
        },
        "fog": {
            CONF_FOG_MODE: "on",
            CONF_FOG_THRESHOLD: 0.6,
            CONF_FOG_CONFIDENCE: 0.0,
            CONF_FOG_RECENT_HOURS: 1.5,
            CONF_FOG_MIN_FACTOR: 0.3,
        },
        "weather": {CONF_WEATHER_MODE: "off", CONF_WEATHER_TRUST: 0.7},
        "archive": {CONF_ARCHIVE_ENABLED: False, CONF_ARCHIVE_RETENTION_DAYS: 100},
    }
    for key, value in changes.items():
        section, field = key.split("__")
        sections[section][field] = value
    return sections


@pytest.mark.usefixtures("entities")
async def test_options_change_inputs_and_settings(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], options_input(level__level_min=1.0, level__level_max=1.0)
    )
    assert result["errors"] == {"base": "level_bounds"}

    result = await hass.config_entries.options.async_configure(result["flow_id"], options_input())
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    options = config_entry.options
    assert options[CONF_FORECAST_TOMORROW] is None  # cleared input overrides the entry data
    assert options[CONF_ACTUAL_ENERGY] is None
    assert options[CONF_ACTUAL_POWER] == [POWER_ENTITY]

    coordinator = config_entry.runtime_data
    assert coordinator.settings.level.days == 10
    assert coordinator.settings.level.by_time is False
    assert coordinator.settings.fog.mode.value == "on"
    assert coordinator.settings.fog.gate.min_confidence is None
    assert coordinator.settings.weather.trust == 0.7
    assert coordinator.archive is None
    fog = hass.states.get("sensor.pv_forecast_correction_fog_factor")
    assert fog.attributes["gate"] == "consistency 0.6"


@pytest.mark.usefixtures("entities")
async def test_options_reject_a_malformed_weather_station(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], options_input(weather__weather_station="not a station")
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_WEATHER_STATION: "invalid_station"}
