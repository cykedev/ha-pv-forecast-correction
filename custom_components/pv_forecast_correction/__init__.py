"""PV Forecast Correction: corrects a PV forecast with the measured production."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_CLEAR_ARCHIVE,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DAYS,
    ATTR_FORECAST_POWER,
    DOMAIN,
    SERVICE_EXPORT,
    SERVICE_LEARN_FROM_HISTORY,
    SERVICE_RESET,
    STORAGE_KEY,
    STORE_VERSION,
)
from .coordinator import CorrectionCoordinator, PvForecastCorrectionConfigEntry, issue_id

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
    """Register the services (once for all entries)."""

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
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PvForecastCorrectionConfigEntry) -> bool:
    """Start the correction for one config entry."""
    coordinator = CorrectionCoordinator(hass, entry)
    await coordinator.async_start()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PvForecastCorrectionConfigEntry) -> bool:
    """Stop the correction and save its state."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: PvForecastCorrectionConfigEntry) -> None:
    """Delete the saved state and open issues; the archive file stays in the config directory."""
    await Store(hass, STORE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}").async_remove()
    for kind in ("forecast", "actual"):
        ir.async_delete_issue(hass, DOMAIN, issue_id(kind, entry))


async def _async_update_listener(
    hass: HomeAssistant, entry: PvForecastCorrectionConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
