"""The weather layer in Home Assistant: fetching DWD MOSMIX, shadow mode, failures."""

from __future__ import annotations

import io
import zipfile
from datetime import date, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pv_forecast_correction.const import (
    CONF_WEATHER_MODE,
    CONF_WEATHER_STATION,
    CONF_WEATHER_TRUST,
    DOMAIN,
    MOSMIX_URL,
    STATION_CATALOGUE_URL,
)
from custom_components.pv_forecast_correction.core.timeline import INTERVAL_HOURS

from .conftest import ENERGY_ENTITY, day_points, local, set_forecasts

DAY = date(2026, 6, 1)
STATION = "X1234"
CATALOGUE = (
    "ID    ICAO NAME                 LAT    LON     ELEV\n"
    "----- ---- -------------------- -----  ------- -----\n"
    "X1234 ---- TESTSTADT             32.52 -117.13    50\n"
)


def element(name: str, values: list[str]) -> str:
    return (
        f'<dwd:Forecast dwd:elementName="{name}">'
        f"<dwd:value>{' '.join(values)}</dwd:value></dwd:Forecast>"
    )


def mosmix(days: list[date], share: float = 1.0) -> bytes:
    """A MOSMIX_L document whose irradiance follows the test forecast times ``share``."""
    hourly: dict[Any, float] = {}
    for day in days:
        for point in day_points(day):
            hour = point.start.replace(minute=0)
            hourly[hour] = hourly.get(hour, 0.0) + point.p50 * INTERVAL_HOURS
    ends = [hour + timedelta(hours=1) for hour in sorted(hourly)]
    rad = [f"{hourly[end - timedelta(hours=1)] * 0.1 * share * 3600:.2f}" for end in ends]
    steps = "".join(
        f"<dwd:TimeStep>{end.isoformat().replace('+00:00', 'Z')}</dwd:TimeStep>" for end in ends
    )
    document = (
        "<kml:kml><dwd:IssueTime>2026-06-01T03:00:00.000Z</dwd:IssueTime>"
        f"<dwd:ForecastTimeSteps>{steps}</dwd:ForecastTimeSteps>"
        + element("Rad1h", rad)
        + element("wwM", ["5"] * len(ends))
        + "</kml:kml>"
    ).encode("latin-1")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"MOSMIX_L_2026060103_{STATION}.kml", document)
    return buffer.getvalue()


@pytest.fixture
def weather_entry(entry_data: dict[str, Any]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="PV Forecast Correction",
        data=entry_data,
        options={
            CONF_WEATHER_MODE: "shadow",
            CONF_WEATHER_STATION: STATION,
            CONF_WEATHER_TRUST: 1.0,
        },
        version=1,
        minor_version=2,
    )


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("berlin")
async def test_weather_is_off_by_default(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    freezer.move_to(local(DAY, 6, 2))
    set_forecasts(hass, DAY)
    hass.states.async_set(ENERGY_ENTITY, "0.0", {"unit_of_measurement": "kWh"})
    await setup(hass, config_entry)
    state = hass.states.get("sensor.pv_forecast_correction_weather_factor")
    assert state.state == "unknown"
    assert state.attributes["status"] == "disabled"
    assert aioclient_mock.call_count == 0


@pytest.mark.usefixtures("berlin")
async def test_shadow_weather_is_fetched_shown_and_archived(
    hass: HomeAssistant,
    weather_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    freezer.move_to(local(DAY, 6, 2))
    aioclient_mock.get(
        MOSMIX_URL.format(station=STATION), content=mosmix([DAY, DAY + timedelta(days=1)])
    )
    aioclient_mock.get(STATION_CATALOGUE_URL, text=CATALOGUE)
    set_forecasts(hass, DAY)
    hass.states.async_set(ENERGY_ENTITY, "0.0", {"unit_of_measurement": "kWh"})
    await setup(hass, weather_entry)
    assert weather_entry.state is ConfigEntryState.LOADED

    state = hass.states.get("sensor.pv_forecast_correction_weather_factor")
    assert state.attributes["status"] == "learning"
    assert state.attributes["station"] == STATION
    assert state.attributes["station_name"] == "Teststadt"
    assert float(state.state) == pytest.approx(1.0, abs=0.01)  # agrees as usual
    assert state.attributes["factors_tomorrow"]
    assert state.attributes["attribution"].startswith("Weather data: Deutscher Wetterdienst")
    coordinator = weather_entry.runtime_data
    await hass.async_block_till_done()
    stats = await hass.async_add_executor_job(coordinator.archive.stats)
    assert stats["weather"]["count"] == 1


@pytest.mark.usefixtures("berlin")
async def test_a_failing_weather_source_leaves_the_correction_running(
    hass: HomeAssistant,
    weather_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    freezer.move_to(local(DAY, 6, 2))
    aioclient_mock.get(MOSMIX_URL.format(station=STATION), status=404)
    aioclient_mock.get(STATION_CATALOGUE_URL, text=CATALOGUE)
    set_forecasts(hass, DAY)
    hass.states.async_set(ENERGY_ENTITY, "0.0", {"unit_of_measurement": "kWh"})
    await setup(hass, weather_entry)
    state = hass.states.get("sensor.pv_forecast_correction_weather_factor")
    assert state.attributes["status"] == "unavailable"
    assert "404" in state.attributes["error"]
    assert hass.states.get("sensor.pv_forecast_correction_tomorrow").state not in (
        "unknown",
        "unavailable",
    )
