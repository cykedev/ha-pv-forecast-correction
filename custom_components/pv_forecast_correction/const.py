"""Constants of the PV Forecast Correction integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "pv_forecast_correction"
DEFAULT_NAME: Final = "PV Forecast Correction"

# Inputs (config entry data, may be overridden by options)
CONF_FORECAST_TODAY: Final = "forecast_today"
CONF_FORECAST_TOMORROW: Final = "forecast_tomorrow"
CONF_ACTUAL_ENERGY: Final = "actual_energy"
CONF_ACTUAL_POWER: Final = "actual_power"
CONF_CURTAILMENT: Final = "curtailment"

# Settings (options)
CONF_LEVEL_ENABLED: Final = "level_enabled"
CONF_LEVEL_DAYS: Final = "level_days"
CONF_LEVEL_SHRINK_DAYS: Final = "level_shrink_days"
CONF_LEVEL_MIN: Final = "level_min"
CONF_LEVEL_MAX: Final = "level_max"
CONF_LEVEL_BY_TIME: Final = "level_by_time"
CONF_FOG_MODE: Final = "fog_mode"
CONF_FOG_THRESHOLD: Final = "fog_threshold"
CONF_FOG_CONFIDENCE: Final = "fog_min_confidence"
CONF_FOG_RECENT_HOURS: Final = "fog_recent_hours"
CONF_FOG_MIN_FACTOR: Final = "fog_min_factor"
CONF_WEATHER_MODE: Final = "weather_mode"
CONF_WEATHER_STATION: Final = "weather_station"
CONF_WEATHER_TRUST: Final = "weather_trust"
CONF_ARCHIVE_ENABLED: Final = "archive_enabled"
CONF_ARCHIVE_RETENTION_DAYS: Final = "archive_retention_days"

# Option form sections
SECTION_INPUTS: Final = "inputs"
SECTION_LEVEL: Final = "level"
SECTION_FOG: Final = "fog"
SECTION_WEATHER: Final = "weather"
SECTION_ARCHIVE: Final = "archive"

DEFAULT_ARCHIVE_RETENTION_DAYS: Final = 400

STORAGE_KEY: Final = DOMAIN
STORE_VERSION: Final = 1
SAVE_DELAY_SECONDS: Final = 60

UPDATE_MINUTES: Final = tuple(range(0, 60, 5))
UPDATE_SECOND: Final = 5
ARCHIVE_EVERY_MINUTES: Final = 15
FORECAST_SETTLE_SECONDS: Final = 2
"""Today's and tomorrow's forecast change one after the other; read them together."""
WARN_AFTER_MINUTES: Final = 10
ISSUE_AFTER_MINUTES: Final = 60

# Weather layer (DWD MOSMIX open data)
MOSMIX_URL: Final = (
    "https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_L/single_stations/"
    "{station}/kml/MOSMIX_L_LATEST_{station}.kmz"
)
STATION_CATALOGUE_URL: Final = (
    "https://www.dwd.de/DE/leistungen/met_verfahren_mosmix/mosmix_stationskatalog.cfg"
    "?view=nasPublication"
)
WEATHER_FETCH_MINUTE: Final = 20
"""MOSMIX is issued every six hours; it is checked once an hour at this minute."""
WEATHER_ATTRIBUTION: Final = "Weather data: Deutscher Wetterdienst (DWD), MOSMIX"
MAX_DOWNLOAD_BYTES: Final = 4_000_000

SERVICE_EXPORT: Final = "export"
SERVICE_RESET: Final = "reset"
SERVICE_LEARN_FROM_HISTORY: Final = "learn_from_history"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_CLEAR_ARCHIVE: Final = "clear_archive"
ATTR_DAYS: Final = "days"
ATTR_FORECAST_POWER: Final = "forecast_power"
