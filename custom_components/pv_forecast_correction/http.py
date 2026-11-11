"""HTTP view: download a copy of an entry's archive."""

from __future__ import annotations

import logging
from http import HTTPStatus

from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView, require_admin
from homeassistant.config_entries import ConfigEntryState
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MAX_ARCHIVE_COPY_BYTES
from .core.archive import ArchiveError

_LOGGER = logging.getLogger(__name__)

_NO_ENTRY = "Config entry not found or not loaded"
_NO_ARCHIVE = "The archive of this entry is switched off or could not be opened"


class ArchiveDownloadView(HomeAssistantView):
    """A consistent copy of the archive as one SQLite file, for administrators."""

    url = f"/api/{DOMAIN}/{{entry_id}}/archive"
    name = f"api:{DOMAIN}:archive"

    @require_admin
    async def get(self, request: web.Request, entry_id: str) -> web.Response:
        hass = request.app[KEY_HASS]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            return self.json_message(_NO_ENTRY, HTTPStatus.NOT_FOUND)
        coordinator = entry.runtime_data
        archive = coordinator.archive
        if archive is None:
            return self.json_message(_NO_ARCHIVE, HTTPStatus.NOT_FOUND)
        try:
            if await hass.async_add_executor_job(archive.content_size) > MAX_ARCHIVE_COPY_BYTES:
                return self.json_message(
                    f"The archive is larger than {MAX_ARCHIVE_COPY_BYTES // 1_000_000} MB;"
                    " take it from a Home Assistant backup instead",
                    HTTPStatus.INSUFFICIENT_STORAGE,
                )
            data = await coordinator.async_archive_copy()
        except ArchiveError as err:
            if coordinator.archive is None:  # unloaded meanwhile
                return self.json_message(_NO_ARCHIVE, HTTPStatus.NOT_FOUND)
            _LOGGER.warning("Archive %s could not be copied for a download: %s", archive.path, err)
            return self.json_message(
                "The archive could not be copied", HTTPStatus.INTERNAL_SERVER_ERROR
            )
        filename = f"{DOMAIN}-{entry_id}-{dt_util.now().strftime('%Y%m%d-%H%M%S')}.db"
        response = web.Response(
            body=data,
            content_type="application/vnd.sqlite3",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            zlib_executor_size=32768,
        )
        response.enable_compression()
        return response
