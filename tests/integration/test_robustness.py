"""Robustness of the Home Assistant side: refresh requests, broken archive folders, a
missing tomorrow forecast, renamed inputs, odd readings, diagnostics and backups."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.pv_forecast_correction import backup
from custom_components.pv_forecast_correction.const import CONF_ACTUAL_ENERGY, DOMAIN
from custom_components.pv_forecast_correction.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import (
    ENERGY_ENTITY,
    TOMORROW_ENTITY,
    local,
    set_forecasts,
    settle,
)

DAY = date(2026, 6, 1)


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: int) -> None:
    for _ in range(minutes // 5):
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


@pytest.mark.usefixtures("berlin")
async def test_update_entity_runs_a_cycle(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(DAY, 10, 2))
    set_forecasts(hass, DAY)
    hass.states.async_set(ENERGY_ENTITY, "1.0", {"unit_of_measurement": "kWh"})
    await setup(hass, config_entry)
    await async_setup_component(hass, "homeassistant", {})
    await hass.services.async_call(
        "homeassistant",
        "update_entity",
        {"entity_id": "sensor.pv_forecast_correction_today"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert config_entry.runtime_data.last_update_success
    assert hass.states.get("sensor.pv_forecast_correction_today").state != STATE_UNAVAILABLE


@pytest.mark.usefixtures("berlin")
async def test_a_file_in_place_of_the_archive_folder_does_not_stop_the_correction(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(DAY, 10, 2))
    set_forecasts(hass, DAY)
    hass.states.async_set(ENERGY_ENTITY, "1.0", {"unit_of_measurement": "kWh"})
    Path(hass.config.path(DOMAIN)).write_text("not a folder")
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data.archive is None
    assert config_entry.runtime_data.archive_error


@pytest.mark.usefixtures("berlin")
async def test_a_missing_tomorrow_keeps_its_last_forecast_and_is_reported(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(DAY, 10, 2))
    set_forecasts(hass, DAY)
    hass.states.async_set(ENERGY_ENTITY, "1.0", {"unit_of_measurement": "kWh"})
    await setup(hass, config_entry)
    before = hass.states.get("sensor.pv_forecast_correction_tomorrow").state
    assert before not in ("unknown", STATE_UNAVAILABLE)

    hass.states.async_set(TOMORROW_ENTITY, STATE_UNAVAILABLE, {})
    await settle(hass)
    await tick(hass, freezer, 65)
    assert hass.states.get("sensor.pv_forecast_correction_tomorrow").state == before
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"forecast_{config_entry.entry_id}")
    assert issue is not None
    assert issue.translation_placeholders["entity_id"] == TOMORROW_ENTITY


@pytest.mark.usefixtures("berlin")
async def test_a_renamed_input_is_followed(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(DAY, 10, 2))
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", "inverter", "energy-today", suggested_object_id=ENERGY_ENTITY.split(".")[1]
    )
    set_forecasts(hass, DAY)
    hass.states.async_set(ENERGY_ENTITY, "1.0", {"unit_of_measurement": "kWh"})
    await setup(hass, config_entry)

    registry.async_update_entity(ENERGY_ENTITY, new_entity_id="sensor.pv_yield_today")
    await hass.async_block_till_done()
    assert config_entry.data[CONF_ACTUAL_ENERGY] == "sensor.pv_yield_today"
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data.inputs.actual_energy == "sensor.pv_yield_today"


@pytest.mark.usefixtures("berlin")
async def test_non_finite_readings_are_ignored(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(DAY, 10, 2))
    set_forecasts(hass, DAY)
    hass.states.async_set(ENERGY_ENTITY, "1.0", {"unit_of_measurement": "kWh"})
    await setup(hass, config_entry)
    hass.states.async_set(ENERGY_ENTITY, "nan", {"unit_of_measurement": "kWh"})
    await tick(hass, freezer, 10)
    hass.states.async_set(ENERGY_ENTITY, "1.5", {"unit_of_measurement": "kWh"})
    await tick(hass, freezer, 10)
    today = hass.states.get("sensor.pv_forecast_correction_today")
    assert today.attributes["actual_so_far"] == pytest.approx(0.5, abs=0.01)
    saved = config_entry.runtime_data.engine.to_dict()
    assert all(row[1] == row[1] for row in saved["bins"]["bins"])  # no NaN


@pytest.mark.usefixtures("berlin")
async def test_diagnostics_of_an_entry_that_is_not_loaded(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["loaded"] is False


@pytest.mark.usefixtures("berlin")
async def test_backup_holds_the_archive_file(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(DAY, 10, 2))
    set_forecasts(hass, DAY)
    hass.states.async_set(ENERGY_ENTITY, "1.0", {"unit_of_measurement": "kWh"})
    await setup(hass, config_entry)
    archive = config_entry.runtime_data.archive
    assert archive is not None
    await backup.async_pre_backup(hass)
    wal = Path(f"{archive.path}-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    await backup.async_post_backup(hass)
