"""Power sensors, units, curtailment and a broken archive."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.pv_forecast_correction.const import (
    CONF_ACTUAL_ENERGY,
    CONF_ACTUAL_POWER,
    CONF_CURTAILMENT,
    CONF_FORECAST_TODAY,
    CONF_FORECAST_TOMORROW,
    DOMAIN,
)

from .conftest import CURTAILMENT_ENTITY, TODAY_ENTITY, TOMORROW_ENTITY, local, set_forecasts

DAY = date(2026, 6, 1)
ROOF = "sensor.roof_power"
GARAGE = "sensor.garage_power"


@pytest.fixture
def power_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="PV Forecast Correction",
        data={
            CONF_FORECAST_TODAY: TODAY_ENTITY,
            CONF_FORECAST_TOMORROW: TOMORROW_ENTITY,
            CONF_ACTUAL_ENERGY: None,
            CONF_ACTUAL_POWER: [ROOF, GARAGE],
            CONF_CURTAILMENT: CURTAILMENT_ENTITY,
        },
    )


async def step(hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: int) -> None:
    for _ in range(minutes // 5):
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


@pytest.mark.usefixtures("berlin")
async def test_power_sensors_are_converted_summed_and_integrated(
    hass: HomeAssistant, power_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(
        local(
            DAY,
            10,
            0,
        )
        + timedelta(seconds=5)
    )
    set_forecasts(hass, DAY)
    hass.states.async_set(ROOF, "2000", {"unit_of_measurement": "W"})
    hass.states.async_set(GARAGE, "1.0", {"unit_of_measurement": "kW"})
    hass.states.async_set(CURTAILMENT_ENTITY, "off")
    power_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(power_entry.entry_id)
    await hass.async_block_till_done()

    await step(hass, freezer, 60)
    today = hass.states.get("sensor.pv_forecast_correction_today")
    assert today.attributes["actual_so_far"] == pytest.approx(3.0, abs=0.01)

    # An unavailable sensor stops the integration of the sum instead of guessing.
    # An unavailable sensor stops the integration of the sum instead of guessing.
    hass.states.async_set(GARAGE, "unavailable")
    await step(hass, freezer, 30)
    today = hass.states.get("sensor.pv_forecast_correction_today")
    assert today.attributes["actual_so_far"] == pytest.approx(3.0, abs=0.01)

    # Curtailment marks the bins it covers.
    hass.states.async_set(GARAGE, "1000", {"unit_of_measurement": "W"})
    hass.states.async_set(CURTAILMENT_ENTITY, "on")
    await step(hass, freezer, 15)
    engine = power_entry.runtime_data.engine
    assert any(item.curtailed for _start, item in engine.bins.items())


@pytest.mark.usefixtures("berlin")
async def test_power_without_unit_is_taken_as_watts(
    hass: HomeAssistant, power_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(DAY, 10, 0) + timedelta(seconds=5))
    set_forecasts(hass, DAY)
    hass.states.async_set(ROOF, "3000", {})
    hass.states.async_set(GARAGE, "0", {"unit_of_measurement": "W"})
    power_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(power_entry.entry_id)
    await hass.async_block_till_done()
    await step(hass, freezer, 60)
    today = hass.states.get("sensor.pv_forecast_correction_today")
    assert today.attributes["actual_so_far"] == pytest.approx(3.0, abs=0.01)


@pytest.mark.usefixtures("berlin")
async def test_unusable_archive_does_not_stop_the_correction(
    hass: HomeAssistant, power_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(DAY, 10, 0) + timedelta(seconds=5))
    set_forecasts(hass, DAY)
    hass.states.async_set(ROOF, "1000", {"unit_of_measurement": "W"})
    hass.states.async_set(GARAGE, "0", {"unit_of_measurement": "W"})
    power_entry.add_to_hass(hass)
    # A directory where the archive file should be makes it impossible to open.
    (lambda path: path.mkdir(parents=True))(
        __import__("pathlib").Path(hass.config.path(DOMAIN, f"{power_entry.entry_id}.db"))
    )
    assert await hass.config_entries.async_setup(power_entry.entry_id)
    await hass.async_block_till_done()
    assert power_entry.state is ConfigEntryState.LOADED
    coordinator = power_entry.runtime_data
    assert coordinator.archive is None
    assert coordinator.archive_error
    assert hass.states.get("sensor.pv_forecast_correction_today").state != "unknown"


@pytest.mark.usefixtures("berlin")
async def test_start_before_the_base_integration_is_no_failure(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState

    freezer.move_to(local(DAY, 9, 0) + timedelta(seconds=5))
    hass.set_state(CoreState.not_running)
    hass.states.async_set("sensor.pv_energy_today", "1.0", {"unit_of_measurement": "kWh"})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.runtime_data.engine.base_failures_today == 0

    set_forecasts(hass, DAY)
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    age = hass.states.get("sensor.pv_forecast_correction_base_age")
    assert age.attributes["status"] == "ok"
    assert age.attributes["failures_today"] == 0


@pytest.mark.usefixtures("berlin")
async def test_inverter_offline_at_night_raises_no_issue(
    hass: HomeAssistant, power_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    from homeassistant.helpers import issue_registry as ir

    freezer.move_to(local(DAY, 21, 0) + timedelta(seconds=5))
    set_forecasts(hass, DAY)
    hass.states.async_set(ROOF, "unavailable")
    hass.states.async_set(GARAGE, "unavailable")
    power_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(power_entry.entry_id)
    await hass.async_block_till_done()
    await step(hass, freezer, 120)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"actual_{power_entry.entry_id}") is None


@pytest.mark.usefixtures("berlin")
async def test_changing_the_production_sensor_does_not_book_its_counter(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    from custom_components.pv_forecast_correction.const import CONF_ACTUAL_ENERGY

    freezer.move_to(local(DAY, 10, 0) + timedelta(seconds=5))
    set_forecasts(hass, DAY)
    hass.states.async_set("sensor.pv_energy_today", "4.2", {"unit_of_measurement": "kWh"})
    hass.states.async_set("sensor.pv_energy_total", "15321", {"unit_of_measurement": "kWh"})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await step(hass, freezer, 10)

    hass.config_entries.async_update_entry(
        config_entry, options={CONF_ACTUAL_ENERGY: "sensor.pv_energy_total"}
    )
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=5))
    hass.states.async_set("sensor.pv_energy_total", "15321.5", {"unit_of_measurement": "kWh"})
    await step(hass, freezer, 10)
    today = hass.states.get("sensor.pv_forecast_correction_today")
    assert today.attributes["actual_so_far"] < 1.0
