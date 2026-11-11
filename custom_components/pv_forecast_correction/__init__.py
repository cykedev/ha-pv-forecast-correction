"""PV Forecast Correction: corrects a PV forecast with the measured production."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import (
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_registry import EventEntityRegistryUpdatedData
from homeassistant.helpers.event import async_track_entity_registry_updated_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_CLEAR_ARCHIVE,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DAYS,
    ATTR_FORECAST_POWER,
    CONF_ACTUAL_ENERGY,
    CONF_ACTUAL_POWER,
    CONF_CURTAILMENT,
    CONF_FORECAST_TODAY,
    CONF_FORECAST_TOMORROW,
    CONF_LEVEL_SHRINK_DAYS,
    DOMAIN,
    SERVICE_EXPORT,
    SERVICE_LEARN_FROM_HISTORY,
    SERVICE_RESET,
    STORAGE_KEY,
    STORE_VERSION,
)
from .coordinator import CorrectionCoordinator, PvForecastCorrectionConfigEntry, issue_id
from .core.level import LevelSettings
from .http import ArchiveDownloadView

PLATFORMS: list[Platform] = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_ENTRY_SCHEMA = vol.Schema({vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string})
_RESET_SCHEMA = _ENTRY_SCHEMA.extend({vol.Optional(ATTR_CLEAR_ARCHIVE, default=False): cv.boolean})
_HISTORY_SCHEMA = _ENTRY_SCHEMA.extend(
    {
        vol.Optional(ATTR_DAYS, default=14): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
        vol.Optional(ATTR_FORECAST_POWER): cv.entity_id,
    }
)


def _coordinator(hass: HomeAssistant, call: ServiceCall) -> CorrectionCoordinator:
    entry = hass.config_entries.async_get_entry(call.data[ATTR_CONFIG_ENTRY_ID])
    if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="entry_not_loaded")
    return entry.runtime_data


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services and the archive download (once for all entries)."""

    async def export(call: ServiceCall) -> ServiceResponse:
        files = await _coordinator(hass, call).async_export()
        return {"files": files}

    async def reset(call: ServiceCall) -> None:
        await _coordinator(hass, call).async_reset(clear_archive=call.data[ATTR_CLEAR_ARCHIVE])

    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT,
        export,
        schema=_ENTRY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def learn_from_history(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass, call)
        if "recorder" not in hass.config.components:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="recorder_unavailable"
            )
        return await coordinator.async_learn_from_history(
            call.data[ATTR_DAYS], call.data.get(ATTR_FORECAST_POWER)
        )

    hass.services.async_register(DOMAIN, SERVICE_RESET, reset, schema=_RESET_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_LEARN_FROM_HISTORY,
        learn_from_history,
        schema=_HISTORY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.http.register_view(ArchiveDownloadView())
    return True


OLD_DEFAULT_SHRINK_DAYS = 2.0
"""Start-up weight before 0.5.0; the default is 1 day since then."""


async def async_migrate_entry(hass: HomeAssistant, entry: PvForecastCorrectionConfigEntry) -> bool:
    """Migrate options of older versions."""
    if entry.version > 1:
        return False  # written by a newer version
    if entry.minor_version < 2:
        options = dict(entry.options)
        # Options saved before 0.5.0 hold the old default start-up weight; follow the new one.
        if options.get(CONF_LEVEL_SHRINK_DAYS) == OLD_DEFAULT_SHRINK_DAYS:
            options[CONF_LEVEL_SHRINK_DAYS] = LevelSettings().shrink_days
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PvForecastCorrectionConfigEntry) -> bool:
    """Start the correction for one config entry."""
    coordinator = CorrectionCoordinator(hass, entry)
    entry.runtime_data = coordinator
    # Also runs when the setup fails part-way, so no listener outlives the entry.
    entry.async_on_unload(coordinator.async_stop)
    await coordinator.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entry.async_on_unload(_follow_renamed_inputs(hass, entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PvForecastCorrectionConfigEntry) -> bool:
    """Stop the correction; its state is saved when the entry unloads."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and entry.disabled_by is not None:
        entry.runtime_data.clear_issues()
    return unloaded


_INPUT_KEYS = (
    CONF_FORECAST_TODAY,
    CONF_FORECAST_TOMORROW,
    CONF_ACTUAL_ENERGY,
    CONF_ACTUAL_POWER,
    CONF_CURTAILMENT,
)


def _renamed(config: Any, old: str, new: str) -> dict[str, Any] | None:
    """``config`` with every input ``old`` replaced by ``new``; None if nothing changed."""
    changed = False
    result = dict(config)
    for key in _INPUT_KEYS:
        value = result.get(key)
        if value == old:
            result[key], changed = new, True
        elif isinstance(value, list) and old in value:
            result[key], changed = [new if item == old else item for item in value], True
    return result if changed else None


@callback
def _follow_renamed_inputs(hass: HomeAssistant, entry: PvForecastCorrectionConfigEntry):
    """Keep the inputs when the user renames one of their entities."""
    inputs = entry.runtime_data.inputs
    tracked = [*inputs.forecast_entities, *inputs.actual_entities]
    if inputs.curtailment:
        tracked.append(inputs.curtailment)

    @callback
    def renamed(event: Event[EventEntityRegistryUpdatedData]) -> None:
        data = event.data
        if data["action"] != "update" or "old_entity_id" not in data:
            return
        old, new = data["old_entity_id"], data["entity_id"]
        new_data = _renamed(entry.data, old, new)
        new_options = _renamed(entry.options, old, new)
        if new_data is None and new_options is None:
            return
        hass.config_entries.async_update_entry(
            entry,
            data=new_data if new_data is not None else entry.data,
            options=new_options if new_options is not None else entry.options,
        )

    return async_track_entity_registry_updated_event(hass, tracked, renamed)


async def async_remove_entry(hass: HomeAssistant, entry: PvForecastCorrectionConfigEntry) -> None:
    """Delete the saved state and open issues; the archive file stays in the config directory."""
    await Store(hass, STORE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}").async_remove()
    for kind in ("forecast", "actual"):
        ir.async_delete_issue(hass, DOMAIN, issue_id(kind, entry))


async def _async_update_listener(
    hass: HomeAssistant, entry: PvForecastCorrectionConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
