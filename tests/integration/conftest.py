"""Fixtures for tests against a Home Assistant test instance."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.pv_forecast_correction.const import (
    CONF_ACTUAL_ENERGY,
    CONF_ACTUAL_POWER,
    CONF_CURTAILMENT,
    CONF_FORECAST_TODAY,
    CONF_FORECAST_TOMORROW,
    DOMAIN,
    FORECAST_SETTLE_SECONDS,
)
from custom_components.pv_forecast_correction.core.forecast_format import (
    ForecastPoint,
    detailed_forecast,
    detailed_hourly,
    energy,
)
from custom_components.pv_forecast_correction.core.timeline import INTERVAL, day_bounds, steps

TZ_NAME = "Europe/Berlin"
TZ = ZoneInfo(TZ_NAME)
TODAY_ENTITY = "sensor.solar_forecast_today"
TOMORROW_ENTITY = "sensor.solar_forecast_tomorrow"
ENERGY_ENTITY = "sensor.pv_energy_today"
POWER_ENTITY = "sensor.pv_power"
CURTAILMENT_ENTITY = "binary_sensor.pv_limited"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None, hass: HomeAssistant, tmp_path: Path
) -> None:
    """Load the integration from custom_components; archives go to a temporary folder."""
    use_temporary_config_dir(hass, tmp_path)


def use_temporary_config_dir(hass: HomeAssistant, tmp_path: Path) -> None:
    """Archive files and exports land in the config folder; keep them out of the shared
    test configuration."""
    hass.config.config_dir = str(tmp_path)


def day_points(day: date, peak: float = 4.0) -> list[ForecastPoint]:
    """Bell-shaped day between 08:00 and 16:00 local time."""
    import math

    start, end = day_bounds(day, TZ)
    points = []
    for interval in steps(start, end, INTERVAL):
        local = interval.astimezone(TZ)
        middle = local.hour + local.minute / 60 + 0.25
        value = peak * math.sin(math.pi * (middle - 8) / 8) if 8 < middle < 16 else 0.0
        points.append(ForecastPoint(interval, value, 0.6 * value, 1.3 * value))
    return points


def forecast_attributes(day: date, peak: float = 4.0) -> dict[str, Any]:
    """Attributes like the Solcast integration publishes them."""
    points = day_points(day, peak)
    return {
        "detailedForecast": detailed_forecast(points, TZ),
        "detailedHourly": detailed_hourly(points, TZ),
        "estimate": energy(points),
        "estimate10": energy(points, "p10"),
        "estimate90": energy(points, "p90"),
        "unit_of_measurement": "kWh",
    }


def set_forecasts(hass: HomeAssistant, day: date, peak: float = 4.0) -> None:
    today = forecast_attributes(day, peak)
    tomorrow = forecast_attributes(day + timedelta(days=1), peak)
    hass.states.async_set(TODAY_ENTITY, str(today["estimate"]), today)
    hass.states.async_set(TOMORROW_ENTITY, str(tomorrow["estimate"]), tomorrow)


async def settle(hass: HomeAssistant) -> None:
    """Forecast changes are read together a moment after the last one."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=FORECAST_SETTLE_SECONDS + 1))
    await hass.async_block_till_done()


def local(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


@pytest.fixture
async def berlin(hass: HomeAssistant) -> HomeAssistant:
    await hass.config.async_set_time_zone(TZ_NAME)
    return hass


@pytest.fixture
def entry_data() -> dict[str, Any]:
    return {
        CONF_FORECAST_TODAY: TODAY_ENTITY,
        CONF_FORECAST_TOMORROW: TOMORROW_ENTITY,
        CONF_ACTUAL_ENERGY: ENERGY_ENTITY,
        CONF_ACTUAL_POWER: [],
        CONF_CURTAILMENT: CURTAILMENT_ENTITY,
    }


@pytest.fixture
def config_entry(entry_data: dict[str, Any]) -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, title="PV Forecast Correction", data=entry_data)
