"""Running over days: learning, errors, archive, services, restarts, repairs."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.pv_forecast_correction.const import DOMAIN
from custom_components.pv_forecast_correction.core.archive import Archive
from custom_components.pv_forecast_correction.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import ENERGY_ENTITY, TODAY_ENTITY, day_points, local, set_forecasts, settle

FIRST = date(2026, 6, 1)
RATIO = 0.7


def produced_until(when: datetime) -> float:
    """Energy (kWh) produced since local midnight when production is RATIO x forecast."""
    day = when.date()
    total = 0.0
    for point in day_points(day):
        start = point.start
        end = start + timedelta(minutes=30)
        seconds = (min(end, when) - start).total_seconds()
        if seconds > 0:
            total += RATIO * point.p50 * seconds / 3600
    return total


async def run_until(hass: HomeAssistant, freezer: FrozenDateTimeFactory, until: datetime) -> None:
    """Advance in 5-minute steps; the energy meter follows RATIO x forecast."""
    now = datetime.now(until.tzinfo)
    while now < until:
        now = min(now + timedelta(minutes=5), until)
        freezer.move_to(now)
        local_now = now.astimezone(until.tzinfo)
        if local_now.hour == 0 and local_now.minute < 5:
            set_forecasts(hass, local_now.date())
        hass.states.async_set(
            ENERGY_ENTITY,
            f"{produced_until(local_now):.4f}",
            {"unit_of_measurement": "kWh"},
        )
        async_fire_time_changed(hass, now)
        await hass.async_block_till_done()


@pytest.mark.usefixtures("berlin")
async def test_days_are_learned_scored_and_archived(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    start = local(FIRST, 0, 1)
    freezer.move_to(start)
    set_forecasts(hass, FIRST)
    hass.states.async_set(ENERGY_ENTITY, "0.0", {"unit_of_measurement": "kWh"})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await run_until(hass, freezer, local(FIRST + timedelta(days=2), 0, 10))

    level = hass.states.get("sensor.pv_forecast_correction_level_factor")
    assert level.attributes["days_used"] == 2
    assert float(level.state) == pytest.approx((2 * RATIO + 1) / 3, abs=0.01)
    assert level.attributes["status"] == "learning"

    base = hass.states.get("sensor.pv_forecast_correction_error_base")
    corrected = hass.states.get("sensor.pv_forecast_correction_error_corrected")
    assert base.attributes["status"] == "ok"
    assert base.attributes["date"] == (FIRST + timedelta(days=1)).isoformat()
    assert float(corrected.state) < float(base.state)
    assert set(base.attributes["rest_of_day"]) == {f"{h:02d}:00" for h in range(9, 15)}
    # Tomorrow's forecast was frozen at 21:00 on the first day, when nothing was learned yet.
    assert corrected.attributes["day_ahead_error"] == base.attributes["day_ahead_error"]
    # Layer B runs in shadow by default: its short-term part follows the measured ratio during
    # the day, which the level factor (still learning) has not reached yet.
    assert corrected.attributes["shadow_rest_of_day_mae"] < float(corrected.state)

    today = hass.states.get("sensor.pv_forecast_correction_today")
    assert today.attributes["level_factor"] == pytest.approx(float(level.state), abs=1e-3)

    coordinator = config_entry.runtime_data
    response = await hass.services.async_call(
        DOMAIN,
        "export",
        {"config_entry_id": config_entry.entry_id},
        blocking=True,
        return_response=True,
    )
    assert len(response["files"]) == 5

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["engine"]["level"]["days_used"] == 2
    assert diagnostics["archive"]["open"]

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    archive = Archive(coordinator.archive_path)
    archive.open()
    assert archive.get_meta("time_zone") == "Europe/Berlin"
    days = [day for day, _report, _stats in archive.day_reports()]
    snapshots = archive.snapshots()
    bins = archive.bins()
    archive.close()
    assert days == [FIRST, FIRST + timedelta(days=1)]
    assert len(snapshots) >= 3
    assert len(bins) > 400

    # A restart restores what was learned.
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    level = hass.states.get("sensor.pv_forecast_correction_level_factor")
    assert level.attributes["days_used"] == 2

    await hass.services.async_call(
        DOMAIN, "reset", {"config_entry_id": config_entry.entry_id}, blocking=True
    )
    level = hass.states.get("sensor.pv_forecast_correction_level_factor")
    assert float(level.state) == 1.0
    assert level.attributes["status"] == "no_history"


@pytest.mark.usefixtures("berlin")
async def test_a_lasting_base_outage_raises_a_repair_issue(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(local(FIRST, 8, 1))
    set_forecasts(hass, FIRST)
    hass.states.async_set(ENERGY_ENTITY, "0.0", {"unit_of_measurement": "kWh"})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set(TODAY_ENTITY, STATE_UNAVAILABLE, {})
    await run_until(hass, freezer, local(FIRST, 9, 10))
    registry = ir.async_get(hass)
    issue = registry.async_get_issue(DOMAIN, f"forecast_{config_entry.entry_id}")
    assert issue is not None
    assert issue.translation_key == "forecast_unavailable"

    set_forecasts(hass, FIRST)
    await settle(hass)
    assert registry.async_get_issue(DOMAIN, f"forecast_{config_entry.entry_id}") is None


@pytest.mark.usefixtures("berlin")
async def test_services_reject_unknown_entries(hass: HomeAssistant, config_entry) -> None:
    from homeassistant.exceptions import ServiceValidationError

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "reset", {"config_entry_id": "unknown"}, blocking=True
        )


@pytest.mark.usefixtures("berlin")
async def test_a_counter_stepping_every_15_minutes_keeps_days_complete(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Like many inverter counters: a new value every 15 minutes, none after sunset."""
    freezer.move_to(local(FIRST, 0, 1))
    set_forecasts(hass, FIRST)
    hass.states.async_set(ENERGY_ENTITY, "0.0", {"unit_of_measurement": "kWh"})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    now = local(FIRST, 0, 1)
    end = local(FIRST + timedelta(days=1), 0, 10)
    value = "0.0"
    while now < end:
        now += timedelta(minutes=1)
        freezer.move_to(now)
        local_now = now.astimezone(end.tzinfo)
        if local_now.hour == 0 and local_now.minute == 0:
            set_forecasts(hass, local_now.date())
        if local_now.minute in (2, 17, 32, 47):
            # The counter reports the energy up to the quarter hour, two minutes late.
            quarter = local_now - timedelta(minutes=2)
            new = f"{produced_until(quarter):.3f}" if quarter.date() == FIRST else "0.000"
            if new != value:
                value = new
                hass.states.async_set(ENERGY_ENTITY, value, {"unit_of_measurement": "kWh"})
        if local_now.minute % 5 == 0:
            async_fire_time_changed(hass, now + timedelta(seconds=5))
        await hass.async_block_till_done()

    stats = config_entry.runtime_data.engine.history[FIRST]
    assert stats.complete
    assert stats.missing == 0
    assert stats.actual_kwh == pytest.approx(RATIO * stats.forecast_kwh, rel=0.02)
