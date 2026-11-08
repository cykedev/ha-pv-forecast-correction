"""SQLite archive: write, read back, prune, export."""

from datetime import date, timedelta

import pytest

from pvfc_core.archive import Archive, ArchiveError, CorrectionRow
from pvfc_core.level import DayStats
from pvfc_core.score import DayCheckpoints, build_report
from pvfc_core.timeline import day_bounds

from .synthetic import TZ, bins_from, curve, forecast

DAY = date(2026, 11, 8)


@pytest.fixture
def archive(tmp_path):
    store = Archive(tmp_path / "archive.db")
    store.open()
    yield store
    store.close()


def test_snapshots_and_bins_round_trip(archive):
    start, end = day_bounds(DAY, TZ)
    base = forecast([DAY])
    archive.add_snapshot(start, base, refreshed=True)
    archive.add_snapshot(start + timedelta(hours=8), forecast([DAY], peak=3.0), refreshed=True)
    bins = bins_from(curve(DAY), 0.8)
    archive.add_bins(bins)
    archive.add_bins(bins[:5])  # replacing is idempotent

    snapshots = archive.snapshots()
    assert len(snapshots) == 2
    assert snapshots[0].forecast == base
    assert snapshots[0].captured_at == start
    assert archive.bins() == bins
    assert len(archive.snapshots(start + timedelta(hours=1), end)) == 1


def test_day_reports_and_corrections(archive, tmp_path):
    stats = DayStats(DAY, 10.0, 12.0, 20, 0, 0)
    report = build_report(
        DAY, DayCheckpoints(), 10.0, {}, complete=True, level_factor=0.8, fog_active=False
    )
    archive.add_day(report, stats)
    start, _ = day_bounds(DAY, TZ)
    archive.add_correction(CorrectionRow(start, 0.8, 1.0, 0.9, 0.5, 0.4, "active", 10.0, 5.0, 12.0))
    [(day, back_report, back_stats)] = archive.day_reports()
    assert day == DAY
    assert back_report == report
    assert back_stats == stats
    files = archive.export_csv(tmp_path / "export")
    assert {f.name for f in files} == {
        "base_forecast.csv",
        "actual.csv",
        "correction.csv",
        "day_report.csv",
    }
    assert "level_factor" in (tmp_path / "export" / "correction.csv").read_text()


def test_prune_and_clear(archive):
    start, _ = day_bounds(DAY, TZ)
    archive.add_snapshot(start, forecast([DAY]), refreshed=True)
    archive.add_bins(bins_from(curve(DAY), 1.0))
    archive.prune(start + timedelta(hours=12))
    assert archive.snapshots() == []
    assert all(b.start >= start + timedelta(hours=12) for b in archive.bins())
    archive.clear()
    assert archive.bins() == []


def test_closed_archive_raises(tmp_path):
    store = Archive(tmp_path / "x.db")
    with pytest.raises(ArchiveError):
        store.bins()


def test_older_files_get_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE actual (bin_start INTEGER PRIMARY KEY, energy_kwh REAL NOT NULL,"
        " covered_s REAL NOT NULL, curtailed INTEGER NOT NULL)"
    )
    conn.execute("INSERT INTO actual VALUES (0, 1.0, 300, 0)")
    conn.commit()
    conn.close()
    store = Archive(path)
    store.open()
    [item] = store.bins()
    store.close()
    assert item.gap_s == 0.0
    assert item.energy_kwh == 1.0
