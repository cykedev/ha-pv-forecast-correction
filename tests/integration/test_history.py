"""Learning the level factor from the recorder's long-term statistics."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.pv_forecast_correction.const import DOMAIN
from custom_components.pv_forecast_correction.core.timeline import day_bounds

from .conftest import (
    ENERGY_ENTITY,
    TODAY_ENTITY,
    TZ,
    local,
    set_forecasts,
    use_temporary_config_dir,
)

FIRST = date(2026, 11, 2)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_mock: Recorder, enable_custom_integrations: None, hass: HomeAssistant, tmp_path: Path
) -> None:
    """The recorder has to start before Home Assistant in these tests."""
    use_temporary_config_dir(hass, tmp_path)


def _meta(entity_id: str, *, has_sum: bool) -> dict:
    return {
        "mean_type": StatisticMeanType.NONE,
        "has_sum": has_sum,
        "name": None,
        "source": "recorder",
        "statistic_id": entity_id,
        "unit_class": "energy",
        "unit_of_measurement": "kWh",
    }


def _import_history(hass: HomeAssistant, days: list[date]) -> None:
    energy_rows, forecast_rows = [], []
    total = 0.0
    start, _ = day_bounds(days[0] - timedelta(days=1), TZ)
    _, end = day_bounds(days[-1], TZ)
    hour = start
    while hour < end:
        local_hour = hour.astimezone(TZ)
        producing = local_hour.date() in days and 8 <= local_hour.hour < 17
        total += 1.0 if producing else 0.0
        energy_rows.append({"start": hour, "state": total, "sum": total})
        if local_hour.date() in days:
            # Revised downward during the day: only the morning value may be used.
            value = 15.0 if local_hour.hour < 10 else 11.0
            forecast_rows.append({"start": hour, "state": value})
        hour += timedelta(hours=1)
    async_import_statistics(hass, _meta(ENERGY_ENTITY, has_sum=True), energy_rows)
    async_import_statistics(hass, _meta(TODAY_ENTITY, has_sum=False), forecast_rows)


@pytest.mark.usefixtures("berlin")
async def test_learns_the_level_factor_from_statistics(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    days = [FIRST, FIRST + timedelta(days=1), FIRST + timedelta(days=2)]
    freezer.move_to(local(FIRST + timedelta(days=3), 10, 0))
    _import_history(hass, days)
    await async_wait_recording_done(hass)

    set_forecasts(hass, FIRST + timedelta(days=3))
    hass.states.async_set(ENERGY_ENTITY, "2.0", {"unit_of_measurement": "kWh"})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    response = await hass.services.async_call(
        DOMAIN,
        "learn_from_history",
        {"config_entry_id": config_entry.entry_id, "days": 5},
        blocking=True,
        return_response=True,
    )
    results = {item["date"]: item for item in response["days"]}
    assert response["imported"] == 3
    for day in days:
        item = results[day.isoformat()]
        assert item["result"] == "imported"
        assert item["actual_kwh"] == pytest.approx(9.0)
        assert item["forecast_kwh"] == pytest.approx(15.0)
    assert results[(FIRST - timedelta(days=1)).isoformat()]["result"] == "no_production"
    # (3 x 9 + 1 x 15) / (3 x 15 + 1 x 15)
    assert response["level_factor"] == pytest.approx(42 / 60, abs=1e-3)

    level = hass.states.get("sensor.pv_forecast_correction_level_factor")
    assert float(level.state) == pytest.approx(42 / 60, abs=1e-3)
    assert level.attributes["imported_days"] == [d.isoformat() for d in days]


@pytest.mark.usefixtures("berlin")
async def test_forecast_power_gives_hours_and_the_time_of_day_curve(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    power_now = "sensor.solar_forecast_power_now"
    days = [FIRST, FIRST + timedelta(days=1), FIRST + timedelta(days=2)]
    freezer.move_to(local(FIRST + timedelta(days=3), 10, 0))
    _import_history(hass, days)
    rows = []
    start, _ = day_bounds(days[0], TZ)
    _, end = day_bounds(days[-1], TZ)
    hour = start
    while hour < end:
        local_hour = hour.astimezone(TZ).hour
        # Forecast of record: 2 kW in the morning (8-10 h), 1.5 kW from 11 h to 16 h.
        watts = 2000.0 if 8 <= local_hour < 11 else 1500.0 if 11 <= local_hour < 17 else 0.0
        rows.append({"start": hour, "mean": watts, "min": watts, "max": watts})
        hour += timedelta(hours=1)
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.ARITHMETIC,
            "has_sum": False,
            "name": None,
            "source": "recorder",
            "statistic_id": power_now,
            "unit_class": "power",
            "unit_of_measurement": "W",
        },
        rows,
    )
    await async_wait_recording_done(hass)

    set_forecasts(hass, FIRST + timedelta(days=3))
    hass.states.async_set(ENERGY_ENTITY, "2.0", {"unit_of_measurement": "kWh"})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    response = await hass.services.async_call(
        DOMAIN,
        "learn_from_history",
        {"config_entry_id": config_entry.entry_id, "days": 5, "forecast_power": power_now},
        blocking=True,
        return_response=True,
    )
    results = {item["date"]: item for item in response["days"]}
    for day in days:
        item = results[day.isoformat()]
        assert item["forecast_from"] == "hourly"
        assert item["forecast_kwh"] == pytest.approx(3 * 2.0 + 6 * 1.5)
    curve = response["curve"]
    # Measured 1 kWh every hour: 50 % of the morning forecast, 67 % of the midday one.
    assert curve["09:00"] < curve["13:00"]
    level = hass.states.get("sensor.pv_forecast_correction_level_factor")
    assert level.attributes["curve"]["09:00"] == pytest.approx(curve["09:00"], abs=1e-3)
