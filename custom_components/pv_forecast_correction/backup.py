"""Backup platform: keep the archive file consistent while a backup copies it."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .core.archive import Archive, ArchiveError

_LOGGER = logging.getLogger(__name__)


def _archives(hass: HomeAssistant) -> list[Archive]:
    return [
        entry.runtime_data.archive
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED and entry.runtime_data.archive is not None
    ]


async def async_pre_backup(hass: HomeAssistant) -> None:
    """Write the archive into its main file and hold it there during the backup."""
    for archive in _archives(hass):
        try:
            await hass.async_add_executor_job(archive.pause_checkpoints)
        except ArchiveError as err:
            _LOGGER.warning("Archive %s not prepared for the backup: %s", archive.path, err)


async def async_post_backup(hass: HomeAssistant) -> None:
    """Let the archive write into its main file again."""
    for archive in _archives(hass):
        try:
            await hass.async_add_executor_job(archive.resume_checkpoints)
        except ArchiveError as err:
            _LOGGER.warning("Archive %s not resumed after the backup: %s", archive.path, err)
