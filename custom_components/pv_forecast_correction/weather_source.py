"""Fetching DWD MOSMIX for the weather layer."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, MAX_DOWNLOAD_BYTES, MOSMIX_URL, STATION_CATALOGUE_URL
from .core.weather import (
    Station,
    WeatherError,
    WeatherForecast,
    nearest_station,
    parse_mosmix,
    parse_stations,
)

_LOGGER = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=60)
_CATALOGUE = f"{DOMAIN}_stations"


@dataclass(slots=True)
class Fetched:
    """A download and the validator to check it for changes next time."""

    forecast: WeatherForecast | None
    last_modified: str | None


async def _download(
    hass: HomeAssistant, url: str, headers: dict[str, str]
) -> tuple[int, bytes, str | None]:
    session = async_get_clientsession(hass)
    try:
        async with session.get(url, headers=headers, timeout=TIMEOUT) as response:
            if response.status == 304:
                return 304, b"", response.headers.get("Last-Modified")
            if response.status != 200:
                raise WeatherError(f"HTTP {response.status} from {url}")
            content = await response.content.read(MAX_DOWNLOAD_BYTES + 1)
            if len(content) > MAX_DOWNLOAD_BYTES:
                raise WeatherError(f"download from {url} is too large")
            return 200, content, response.headers.get("Last-Modified")
    except (aiohttp.ClientError, TimeoutError) as err:
        raise WeatherError(f"{url}: {err}") from err


async def async_fetch_mosmix(
    hass: HomeAssistant, station: str, last_modified: str | None = None
) -> Fetched:
    """The latest MOSMIX run of a station; ``forecast`` is None when it has not changed."""
    headers = {"If-Modified-Since": last_modified} if last_modified else {}
    status, content, modified = await _download(hass, MOSMIX_URL.format(station=station), headers)
    if status == 304:
        return Fetched(None, modified or last_modified)
    forecast = await hass.async_add_executor_job(parse_mosmix, content, station)
    return Fetched(forecast, modified)


async def async_stations(hass: HomeAssistant) -> list[Station]:
    """The DWD station catalogue (downloaded once per start)."""
    cached: list[Station] | None = hass.data.get(_CATALOGUE)
    if cached:
        return cached
    _, content, _ = await _download(hass, STATION_CATALOGUE_URL, {})
    stations = parse_stations(content.decode("latin-1"))
    if not stations:
        raise WeatherError("the DWD station catalogue is empty")
    hass.data[_CATALOGUE] = stations
    return stations


async def async_nearest_station(hass: HomeAssistant) -> Station:
    """The MOSMIX station nearest to Home Assistant's home location."""
    stations = await async_stations(hass)
    return nearest_station(stations, hass.config.latitude, hass.config.longitude)


async def async_station(hass: HomeAssistant, station_id: str) -> Station | None:
    """Catalogue entry of a station, with its distance from home (None if unknown)."""
    try:
        stations = await async_stations(hass)
    except WeatherError:
        return None
    for station in stations:
        if station.id == station_id:
            return nearest_station([station], hass.config.latitude, hass.config.longitude)
    return None
