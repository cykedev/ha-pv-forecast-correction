"""tools/replay.py runs over an archive file and prints the verdicts."""

import importlib.util
from pathlib import Path

from pvfc_core.archive import Archive

from ..core.synthetic import bins_from, curve, daily_snapshots, day_range

TOOL = Path(__file__).resolve().parents[2] / "tools" / "replay.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("replay_tool", TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_replay_prints_tables_and_verdicts(tmp_path, capsys):
    from datetime import date

    days = day_range(date(2026, 6, 1), 20)
    archive = Archive(tmp_path / "archive.db")
    archive.open()
    archive.set_meta("time_zone", "Europe/Berlin")
    archive.set_meta("settings", '{"level": {"days": 7}, "fog": {"min_factor": 0.3}}')
    for snapshot in daily_snapshots(days):
        archive.add_snapshot(snapshot.captured_at, snapshot.forecast, refreshed=True)
    points = [p for day in days for p in curve(day)]
    archive.add_bins(bins_from(points, 0.75))
    archive.close()

    assert load_tool().main([str(tmp_path / "archive.db"), "--days"]) == 0
    output = capsys.readouterr().out
    assert "20 complete days" in output
    assert "## Layer A (level) against the base" in output
    assert "Verdict: PASS" in output
    assert "## Days" in output
    assert "Level factor: 7 days" in output
    assert "lowest factor 0.3" in output


def test_empty_archive_reports_no_days(tmp_path, capsys):
    archive = Archive(tmp_path / "empty.db")
    archive.open()
    archive.close()
    assert load_tool().main([str(tmp_path / "empty.db"), "--tz", "UTC"]) == 1
    assert "No complete days" in capsys.readouterr().out


def test_archive_without_time_zone_needs_tz(tmp_path):
    import pytest

    archive = Archive(tmp_path / "no-tz.db")
    archive.open()
    archive.close()
    with pytest.raises(SystemExit):
        load_tool().main([str(tmp_path / "no-tz.db")])
