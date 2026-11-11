"""Archive download: a consistent copy of the archive file, for administrators only."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import tempfile
import threading
from functools import partial
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.pv_forecast_correction.backup import async_post_backup, async_pre_backup
from custom_components.pv_forecast_correction.const import CONF_ARCHIVE_ENABLED, DOMAIN
from custom_components.pv_forecast_correction.core.archive import Archive, ArchiveError
from custom_components.pv_forecast_correction.core.forecast_format import Forecast

from .conftest import ENERGY_ENTITY, day_points, local, set_forecasts, settle

pytestmark = pytest.mark.usefixtures("berlin")


def url(entry_id: str) -> str:
    return f"/api/{DOMAIN}/{entry_id}/archive"


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temporary files of the download go here, so that leftovers can be seen."""
    folder = tmp_path / "scratch"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    set_forecasts(hass, dt_util.now().date())
    hass.states.async_set(ENERGY_ENTITY, "0.0", {"unit_of_measurement": "kWh"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await settle(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def inspect(data: bytes, folder: Path) -> dict[str, Any]:
    """Open downloaded bytes as an SQLite file."""
    path = folder / "downloaded.db"
    path.write_bytes(data)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
            "journal": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "tables": {r[0] for r in conn.execute("SELECT name FROM sqlite_master")},
            "time_zone": conn.execute("SELECT value FROM meta WHERE key = 'time_zone'").fetchone(),
            "snapshots": conn.execute("SELECT COUNT(*) FROM base_snapshot").fetchone()[0],
            "files": sorted(f.name for f in folder.iterdir()),
        }
    finally:
        conn.close()


async def test_administrator_gets_a_consistent_copy(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    scratch: Path,
    tmp_path: Path,
) -> None:
    await setup(hass, config_entry)
    client = await hass_client()

    response = await client.get(url(config_entry.entry_id))

    assert response.status == HTTPStatus.OK
    assert response.content_type == "application/vnd.sqlite3"
    assert re.fullmatch(
        rf'attachment; filename="{DOMAIN}-{config_entry.entry_id}-\d{{8}}-\d{{6}}\.db"',
        response.headers["Content-Disposition"],
    )
    assert response.headers["Content-Encoding"] in ("gzip", "deflate")
    data = await response.read()
    target = tmp_path / "result"
    target.mkdir()
    found = inspect(data, target)
    assert found["integrity"] == "ok"
    assert found["journal"] == "delete"
    assert {"meta", "base_snapshot", "base_value", "actual", "day_report"} <= found["tables"]
    assert found["time_zone"] == ("Europe/Berlin",)
    assert found["snapshots"] >= 1
    assert found["files"] == ["downloaded.db"]  # readable without a write-ahead log
    assert list(scratch.iterdir()) == []

    plain = await client.get(url(config_entry.entry_id), headers={"Accept-Encoding": "identity"})
    assert "Content-Encoding" not in plain.headers
    assert int(plain.headers["Content-Length"]) == len(data)
    assert await plain.read() == data


async def test_only_administrators_may_download(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    hass_read_only_access_token: str,
    scratch: Path,
) -> None:
    await setup(hass, config_entry)

    read_only = await hass_client(hass_read_only_access_token)
    assert (await read_only.get(url(config_entry.entry_id))).status == HTTPStatus.UNAUTHORIZED
    anonymous = await hass_client_no_auth()
    assert (await anonymous.get(url(config_entry.entry_id))).status == HTTPStatus.UNAUTHORIZED
    assert list(scratch.iterdir()) == []


async def test_entries_that_cannot_be_downloaded(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    entry_data: dict[str, Any],
    hass_client: ClientSessionGenerator,
) -> None:
    await setup(hass, config_entry)
    other = MockConfigEntry(domain="other_domain", title="Other")
    other.add_to_hass(hass)
    other.mock_state(hass, ConfigEntryState.LOADED)
    switched_off = MockConfigEntry(
        domain=DOMAIN,
        title="Without archive",
        data={**entry_data, CONF_ARCHIVE_ENABLED: False},
        unique_id="without-archive",
    )
    await setup(hass, switched_off)
    assert switched_off.runtime_data.archive is None
    client = await hass_client()

    async def refused(entry_id: str) -> str:
        response = await client.get(url(entry_id))
        assert response.status == HTTPStatus.NOT_FOUND
        return (await response.json())["message"]

    assert await refused("unknown") == "Config entry not found or not loaded"
    assert await refused(other.entry_id) == "Config entry not found or not loaded"
    assert "switched off" in await refused(switched_off.entry_id)

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert await refused(config_entry.entry_id) == "Config entry not found or not loaded"


async def test_archive_that_could_not_be_opened(
    hass: HomeAssistant, config_entry: MockConfigEntry, hass_client: ClientSessionGenerator
) -> None:
    # A directory where the archive file should be makes it impossible to open.
    Path(hass.config.path(DOMAIN, f"{config_entry.entry_id}.db")).mkdir(parents=True)
    await setup(hass, config_entry)
    assert config_entry.runtime_data.archive is None
    client = await hass_client()

    assert (await client.get(url(config_entry.entry_id))).status == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize("error", [ArchiveError("disk full"), OSError("disk full")])
async def test_failed_copy_is_logged_and_leaves_nothing_behind(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    await setup(hass, config_entry)
    client = await hass_client()

    def broken(self: Archive, path: Path) -> None:
        Path(path).write_bytes(b"half a file")
        raise error

    monkeypatch.setattr(Archive, "copy_to", broken)
    response = await client.get(url(config_entry.entry_id))

    assert response.status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert "disk full" not in await response.text()
    assert "could not be copied for a download: disk full" in caplog.text
    assert list(scratch.iterdir()) == []


async def test_entry_unloaded_during_the_copy(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup(hass, config_entry)
    client = await hass_client()
    coordinator = config_entry.runtime_data

    def closed_meanwhile(self: Archive, path: Path) -> None:
        coordinator.archive = None
        raise ArchiveError("archive is not open")

    monkeypatch.setattr(Archive, "copy_to", closed_meanwhile)
    response = await client.get(url(config_entry.entry_id))

    assert response.status == HTTPStatus.NOT_FOUND
    assert "could not be copied" not in caplog.text


async def test_archive_above_the_limit_is_refused(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await setup(hass, config_entry)
    client = await hass_client()

    monkeypatch.setattr(Archive, "content_size", lambda self: 150_000_001)
    response = await client.get(url(config_entry.entry_id))
    assert response.status == HTTPStatus.INSUFFICIENT_STORAGE
    message = (await response.json())["message"]
    assert "150 MB" in message
    assert "backup" in message
    assert list(scratch.iterdir()) == []

    monkeypatch.setattr(Archive, "content_size", lambda self: 150_000_000)
    assert (await client.get(url(config_entry.entry_id))).status == HTTPStatus.OK


async def test_cancelled_copy_leaves_nothing_behind(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await setup(hass, config_entry)
    written, release = threading.Event(), threading.Event()
    copy_to = Archive.copy_to

    def slow(self: Archive, path: Path) -> None:
        copy_to(self, path)
        written.set()
        release.wait(10)

    monkeypatch.setattr(Archive, "copy_to", slow)
    task = hass.async_create_task(config_entry.runtime_data.async_archive_copy())
    assert await hass.async_add_executor_job(written.wait, 10)
    assert [f.name for folder in scratch.iterdir() for f in folder.iterdir()] == ["archive.db"]

    task.cancel()  # the client went away while the copy was being made
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    for _ in range(100):
        if not list(scratch.iterdir()):
            break
        await asyncio.sleep(0.05)
    assert list(scratch.iterdir()) == []


async def test_download_while_a_backup_holds_the_file(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    tmp_path: Path,
) -> None:
    await setup(hass, config_entry)
    client = await hass_client()
    archive = config_entry.runtime_data.archive
    today = dt_util.now().date()

    await async_pre_backup(hass)
    try:
        before = archive.path.read_bytes()
        # Written while the backup runs: it waits in the write-ahead log.
        await hass.async_add_executor_job(archive.set_meta, "probe", "during the backup")
        response = await client.get(url(config_entry.entry_id))
        assert response.status == HTTPStatus.OK
        data = await response.read()
        for minute in range(20):  # enough to move the log into the file, were that allowed
            await hass.async_add_executor_job(
                partial(
                    archive.add_snapshot,
                    local(today, 12, minute),
                    Forecast(tuple(day_points(today))),
                    refreshed=True,
                )
            )
        assert archive.path.read_bytes() == before  # the file a backup copies stays as it is
    finally:
        await async_post_backup(hass)

    target = tmp_path / "result"
    target.mkdir()
    assert inspect(data, target)["integrity"] == "ok"
    conn = sqlite3.connect(f"file:{target / 'downloaded.db'}?mode=ro", uri=True)
    try:
        probe = conn.execute("SELECT value FROM meta WHERE key = 'probe'").fetchone()
    finally:
        conn.close()
    assert probe == ("during the backup",)  # the copy includes what is still in the log
