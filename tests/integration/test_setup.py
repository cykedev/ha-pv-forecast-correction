"""Setting up an entry, the published sensors and unloading."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast_correction.const import CONF_LEVEL_SHRINK_DAYS, DOMAIN

from .conftest import (
    CURTAILMENT_ENTITY,
    ENERGY_ENTITY,
    TODAY_ENTITY,
    forecast_attributes,
    local,
    set_forecasts,
)

DAY = date(2026, 6, 1)
SENSORS = (
    "today",
    "remaining_today",
    "tomorrow",
    "level_factor",
    "fog_factor",
    "error_base",
    "error_corrected",
    "base_age",
)


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("berlin")
async def test_sensors_publish_the_solcast_format(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(DAY, 10, 2))
    set_forecasts(hass, DAY)
    hass.states.async_set(ENERGY_ENTITY, "1.5", {"unit_of_measurement": "kWh"})
    hass.states.async_set(CURTAILMENT_ENTITY, "off")
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    registry = er.async_get(hass)
    for key in SENSORS:
        entity_id = f"sensor.pv_forecast_correction_{key}"
        assert registry.async_get(entity_id) is not None, entity_id

    expected = forecast_attributes(DAY)
    today = hass.states.get("sensor.pv_forecast_correction_today")
    assert float(today.state) == pytest.approx(expected["estimate"], abs=0.01)
    assert today.attributes["base_estimate"] == pytest.approx(expected["estimate"], abs=1e-3)
    assert today.attributes["level_factor"] == 1.0
    first = today.attributes["detailedForecast"][0]
    assert isinstance(first["period_start"], datetime)
    assert set(first) == {"period_start", "pv_estimate", "pv_estimate10", "pv_estimate90"}
    assert len(today.attributes["detailedHourly"]) == 24

    remaining = hass.states.get("sensor.pv_forecast_correction_remaining_today")
    assert 0 < float(remaining.state) < float(today.state)
    assert "estimate10" in remaining.attributes

    tomorrow = hass.states.get("sensor.pv_forecast_correction_tomorrow")
    assert float(tomorrow.state) == pytest.approx(expected["estimate"], abs=0.01)

    level = hass.states.get("sensor.pv_forecast_correction_level_factor")
    assert float(level.state) == 1.0
    assert level.attributes["status"] == "no_history"

    fog = hass.states.get("sensor.pv_forecast_correction_fog_factor")
    assert float(fog.state) == 1.0
    assert fog.attributes["mode"] == "shadow"
    assert fog.attributes["gate"] == "confident miss 0.7 / 0.5"

    errors = hass.states.get("sensor.pv_forecast_correction_error_corrected")
    assert errors.attributes["status"] == "no_report"

    age = hass.states.get("sensor.pv_forecast_correction_base_age")
    assert age.attributes["status"] == "ok"

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.usefixtures("berlin")
async def test_unavailable_base_keeps_the_last_forecast(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(DAY, 9, 1))
    set_forecasts(hass, DAY)
    hass.states.async_set(ENERGY_ENTITY, "1.0", {"unit_of_measurement": "kWh"})
    await setup(hass, config_entry)
    before = hass.states.get("sensor.pv_forecast_correction_tomorrow").state

    hass.states.async_set(TODAY_ENTITY, STATE_UNAVAILABLE, {})
    await hass.async_block_till_done()

    assert hass.states.get("sensor.pv_forecast_correction_tomorrow").state == before
    age = hass.states.get("sensor.pv_forecast_correction_base_age")
    assert age.attributes["status"] == "unavailable"
    assert age.attributes["failures_today"] == 1


@pytest.mark.usefixtures("berlin")
async def test_missing_inputs_do_not_break_setup(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(DAY, 9, 1))
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    today = hass.states.get("sensor.pv_forecast_correction_today")
    assert today.state == "unknown"


@pytest.mark.usefixtures("berlin")
async def test_options_with_the_old_default_start_up_weight_are_migrated(
    hass: HomeAssistant, entry_data, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(DAY, 10, 2))
    set_forecasts(hass, DAY)
    hass.states.async_set(ENERGY_ENTITY, "1.0", {"unit_of_measurement": "kWh"})
    old = MockConfigEntry(
        domain=DOMAIN, data=entry_data, options={CONF_LEVEL_SHRINK_DAYS: 2.0}, version=1
    )
    chosen = MockConfigEntry(
        domain=DOMAIN,
        data={**entry_data, "forecast_today": "sensor.other_forecast"},
        options={CONF_LEVEL_SHRINK_DAYS: 3.0},
        version=1,
    )
    await setup(hass, old)
    chosen.add_to_hass(hass)
    await hass.config_entries.async_setup(chosen.entry_id)
    await hass.async_block_till_done()
    assert old.minor_version == 2
    assert old.options[CONF_LEVEL_SHRINK_DAYS] == 1.0
    assert chosen.options[CONF_LEVEL_SHRINK_DAYS] == 3.0  # chosen by the user: kept
