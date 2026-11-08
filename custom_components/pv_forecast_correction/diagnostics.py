"""Diagnostics: configuration, engine state and archive status of one entry."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .config import merged, settings_dict
from .coordinator import PvForecastCorrectionConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PvForecastCorrectionConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    engine = coordinator.engine
    return {
        "config": merged(entry.data, entry.options),
        "settings": settings_dict(coordinator.settings),
        "archive": {
            "enabled": coordinator.archive_enabled,
            "open": coordinator.archive is not None,
            "path": str(coordinator.archive_path),
            "last_error": coordinator.archive_error,
        },
        "last_error": coordinator.last_error,
        "engine": {
            "day": None if engine.day is None else engine.day.isoformat(),
            "last_update": None if engine.last_update is None else engine.last_update.isoformat(),
            "base_valid": engine.base_valid,
            "base_refreshed_at": None
            if engine.base_refreshed_at is None
            else engine.base_refreshed_at.isoformat(),
            "base_intervals": len(engine.base.points),
            "base_has_band": engine.base.has_bands,
            "forecast_of_record_intervals": len(engine.record),
            "measured_bins": len(engine.bins),
            "meter_source": engine.meter_source,
            "rejected_readings": engine.rejected_readings,
            "level": engine.level.to_dict(),
            "fog": engine.fog.to_dict(),
            "checkpoints": engine.checkpoints.to_dict(),
            "history": [engine.history[day].to_dict() for day in sorted(engine.history)],
            "report": None if engine.report is None else engine.report.to_dict(),
        },
    }
