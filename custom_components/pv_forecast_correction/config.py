"""Translate a config entry into input entities and correction settings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from .const import (
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
    DEFAULT_ARCHIVE_RETENTION_DAYS,
)
from .core import FogGate, FogMode, FogSettings, LevelSettings, Settings

_LEVEL = LevelSettings()
_FOG = FogSettings()

DEFAULTS: dict[str, Any] = {
    CONF_LEVEL_ENABLED: True,
    CONF_LEVEL_DAYS: _LEVEL.days,
    CONF_LEVEL_SHRINK_DAYS: _LEVEL.shrink_days,
    CONF_LEVEL_MIN: _LEVEL.min_factor,
    CONF_LEVEL_MAX: _LEVEL.max_factor,
    CONF_LEVEL_BY_TIME: _LEVEL.by_time,
    CONF_FOG_MODE: _FOG.mode.value,
    CONF_FOG_THRESHOLD: _FOG.gate.threshold,
    CONF_FOG_CONFIDENCE: _FOG.gate.min_confidence,
    CONF_FOG_RECENT_HOURS: _FOG.recent_hours,
    CONF_FOG_MIN_FACTOR: _FOG.min_factor,
    CONF_ARCHIVE_ENABLED: True,
    CONF_ARCHIVE_RETENTION_DAYS: DEFAULT_ARCHIVE_RETENTION_DAYS,
}


@dataclass(frozen=True, slots=True)
class Inputs:
    """Entities the integration reads."""

    forecast_today: str
    forecast_tomorrow: str | None
    actual_energy: str | None
    actual_power: tuple[str, ...]
    curtailment: str | None

    @property
    def forecast_entities(self) -> list[str]:
        return [e for e in (self.forecast_today, self.forecast_tomorrow) if e]

    @property
    def actual_entities(self) -> list[str]:
        return [self.actual_energy] if self.actual_energy else list(self.actual_power)


def merged(data: Mapping[str, Any], options: Mapping[str, Any]) -> dict[str, Any]:
    """Defaults, overridden by the entry data, overridden by the options."""
    return {**DEFAULTS, **data, **options}


def inputs_from(config: Mapping[str, Any]) -> Inputs:
    power = config.get(CONF_ACTUAL_POWER) or ()
    if isinstance(power, str):
        power = (power,)
    return Inputs(
        forecast_today=config[CONF_FORECAST_TODAY],
        forecast_tomorrow=config.get(CONF_FORECAST_TOMORROW) or None,
        actual_energy=config.get(CONF_ACTUAL_ENERGY) or None,
        actual_power=tuple(power),
        curtailment=config.get(CONF_CURTAILMENT) or None,
    )


def _optional(value: Any) -> float | None:
    if value is None or value == "":
        return None
    number = float(value)
    return number if number > 0 else None


def settings_from(config: Mapping[str, Any]) -> Settings:
    """Core settings; invalid values fall back to the defaults."""
    try:
        level = LevelSettings(
            days=int(config[CONF_LEVEL_DAYS]),
            shrink_days=float(config[CONF_LEVEL_SHRINK_DAYS]),
            min_factor=float(config[CONF_LEVEL_MIN]),
            max_factor=float(config[CONF_LEVEL_MAX]),
            by_time=bool(config[CONF_LEVEL_BY_TIME]),
        )
    except (KeyError, TypeError, ValueError):
        level = LevelSettings()
    try:
        fog = FogSettings(
            mode=FogMode(config[CONF_FOG_MODE]),
            gate=FogGate(
                threshold=_optional(config[CONF_FOG_THRESHOLD]),
                min_confidence=_optional(config[CONF_FOG_CONFIDENCE]),
            ),
            recent_hours=float(config[CONF_FOG_RECENT_HOURS]),
            min_factor=float(config[CONF_FOG_MIN_FACTOR]),
        )
    except (KeyError, TypeError, ValueError):
        fog = FogSettings()
    return Settings(level_enabled=bool(config[CONF_LEVEL_ENABLED]), level=level, fog=fog)


def settings_dict(settings: Settings) -> dict[str, Any]:
    """Settings as plain JSON-compatible values (archive metadata, diagnostics)."""
    data = asdict(settings)
    data["fog"]["mode"] = settings.fog.mode.value
    return data
