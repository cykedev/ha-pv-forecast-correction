"""Archive: every base forecast snapshot, the measured energy and the applied factors.

The base forecast's interval lists are not kept by Home Assistant's recorder and weather
model runs are not kept by their publishers. Without this archive no variant of the
correction could ever be evaluated honestly against what was known at the time. The
replay tool reads the same file.

SQLite, stdlib only. Times are stored as Unix seconds (UTC).
"""

from __future__ import annotations

import csv
import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .forecast_format import Forecast, ForecastPoint
from .level import DayStats
from .score import DayReport
from .timeline import as_utc
from .weather import WeatherForecast, WeatherPoint

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS base_snapshot (
    id          INTEGER PRIMARY KEY,
    captured_at INTEGER NOT NULL,
    refreshed   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS base_snapshot_time ON base_snapshot (captured_at);
CREATE TABLE IF NOT EXISTS base_value (
    snapshot_id  INTEGER NOT NULL REFERENCES base_snapshot (id) ON DELETE CASCADE,
    period_start INTEGER NOT NULL,
    p50          REAL NOT NULL,
    p10          REAL,
    p90          REAL,
    PRIMARY KEY (snapshot_id, period_start)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS actual (
    bin_start  INTEGER PRIMARY KEY,
    energy_kwh REAL NOT NULL,
    covered_s  REAL NOT NULL,
    curtailed  INTEGER NOT NULL,
    gap_s      REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS correction (
    ts            INTEGER PRIMARY KEY,
    level_factor  REAL NOT NULL,
    fog_applied   REAL NOT NULL,
    fog_shadow    REAL,
    k_morning     REAL,
    k_recent      REAL,
    fog_reason    TEXT,
    today_kwh     REAL,
    remaining_kwh REAL,
    tomorrow_kwh  REAL
);
CREATE TABLE IF NOT EXISTS day_report (
    day    TEXT PRIMARY KEY,
    report TEXT NOT NULL,
    stats  TEXT
);
CREATE TABLE IF NOT EXISTS weather_snapshot (
    id          INTEGER PRIMARY KEY,
    captured_at INTEGER NOT NULL,
    issued_at   INTEGER NOT NULL,
    station     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS weather_snapshot_time ON weather_snapshot (captured_at);
CREATE TABLE IF NOT EXISTS weather_value (
    snapshot_id INTEGER NOT NULL REFERENCES weather_snapshot (id) ON DELETE CASCADE,
    hour_start  INTEGER NOT NULL,
    irradiance  REAL,
    relative    REAL,
    sunshine    REAL,
    fog         REAL,
    low_cloud   REAL,
    extra       TEXT,
    PRIMARY KEY (snapshot_id, hour_start)
) WITHOUT ROWID;
"""

WEATHER_HORIZON = timedelta(hours=72)
"""Only the next three days of a weather run are archived (a run covers ten)."""


class ArchiveError(Exception):
    """The archive file cannot be used."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The base forecast as it was known at ``captured_at``."""

    captured_at: datetime
    forecast: Forecast
    refreshed: bool = True


@dataclass(frozen=True, slots=True)
class WeatherSnapshot:
    """A weather run as it was known at ``captured_at``."""

    captured_at: datetime
    forecast: WeatherForecast


@dataclass(frozen=True, slots=True)
class MeasuredBin:
    """Measured energy of one 5-minute bin."""

    start: datetime
    energy_kwh: float
    covered_s: float
    curtailed: bool
    gap_s: float = 0.0


@dataclass(frozen=True, slots=True)
class CorrectionRow:
    """Factors and totals published at one moment."""

    ts: datetime
    level_factor: float
    fog_applied: float
    fog_shadow: float | None
    k_morning: float | None
    k_recent: float | None
    fog_reason: str | None
    today_kwh: float | None
    remaining_kwh: float | None
    tomorrow_kwh: float | None


def _ts(value: datetime) -> int:
    return int(as_utc(value).timestamp())


def _dt(value: int) -> datetime:
    return datetime.fromtimestamp(int(value), UTC)


class Archive:
    """Thread-safe access to one archive file. All methods block (run them in an executor)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def open(self) -> None:
        """Open or create the file and its schema."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            raise ArchiveError(str(err)) from err
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(actual)")}
            if "gap_s" not in columns:  # files written before the column existed
                conn.execute("ALTER TABLE actual ADD COLUMN gap_s REAL NOT NULL DEFAULT 0")
            weather_columns = {row[1] for row in conn.execute("PRAGMA table_info(weather_value)")}
            if "extra" not in weather_columns:  # files written by 0.5.0 to 0.5.2
                conn.execute("ALTER TABLE weather_value ADD COLUMN extra TEXT")
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row[0]) > SCHEMA_VERSION:
                raise ArchiveError(f"archive schema {row[0]} is newer than {SCHEMA_VERSION}")
        except ArchiveError:
            if conn is not None:
                conn.close()
            raise
        except sqlite3.Error as err:
            if conn is not None:
                conn.close()
            raise ArchiveError(str(err)) from err
        self._conn = conn

    def pause_checkpoints(self) -> None:
        """Write everything into the main file and keep it unchanged until
        ``resume_checkpoints`` (a backup copies the file meanwhile)."""
        try:
            with self._lock:
                if self._conn is not None:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    self._conn.execute("PRAGMA wal_autocheckpoint=0")
        except sqlite3.Error as err:
            raise ArchiveError(str(err)) from err

    def resume_checkpoints(self) -> None:
        try:
            with self._lock:
                if self._conn is not None:
                    self._conn.execute("PRAGMA wal_autocheckpoint=1000")
        except sqlite3.Error as err:
            raise ArchiveError(str(err)) from err

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        if self._conn is None:
            raise ArchiveError("archive is not open")
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        if self._conn is None:
            raise ArchiveError("archive is not open")
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------ writing

    def set_meta(self, key: str, value: str) -> None:
        """Store a piece of metadata, e.g. the time zone of the local days."""
        try:
            with self._transaction() as conn:
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        except sqlite3.Error as err:
            raise ArchiveError(str(err)) from err

    def get_meta(self, key: str) -> str | None:
        rows = self._query("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0][0] if rows else None

    def add_snapshot(self, captured_at: datetime, forecast: Forecast, *, refreshed: bool) -> int:
        """Store a full copy of the base forecast."""
        with self._transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO base_snapshot (captured_at, refreshed) VALUES (?, ?)",
                (_ts(captured_at), int(refreshed)),
            )
            snapshot_id = int(cursor.lastrowid or 0)
            conn.executemany(
                "INSERT INTO base_value (snapshot_id, period_start, p50, p10, p90)"
                " VALUES (?, ?, ?, ?, ?)",
                [(snapshot_id, _ts(p.start), p.p50, p.p10, p.p90) for p in forecast.points],
            )
        return snapshot_id

    def add_weather(self, captured_at: datetime, forecast: WeatherForecast) -> int:
        """Store a weather run (its next three days)."""
        horizon = as_utc(captured_at) + WEATHER_HORIZON
        with self._transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO weather_snapshot (captured_at, issued_at, station) VALUES (?, ?, ?)",
                (_ts(captured_at), _ts(forecast.issued_at), forecast.station),
            )
            snapshot_id = int(cursor.lastrowid or 0)
            conn.executemany(
                "INSERT INTO weather_value (snapshot_id, hour_start, irradiance, relative,"
                " sunshine, fog, low_cloud, extra) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        snapshot_id,
                        _ts(p.start),
                        p.irradiance,
                        p.relative,
                        p.sunshine,
                        p.fog,
                        p.low_cloud,
                        json.dumps(dict(p.extra)) if p.extra else None,
                    )
                    for p in forecast.points
                    if p.start < horizon
                ],
            )
        return snapshot_id

    def add_bins(self, bins: Iterable[MeasuredBin]) -> None:
        """Insert or replace measured bins."""
        with self._transaction() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO actual"
                " (bin_start, energy_kwh, covered_s, curtailed, gap_s) VALUES (?, ?, ?, ?, ?)",
                [
                    (_ts(b.start), b.energy_kwh, b.covered_s, int(b.curtailed), b.gap_s)
                    for b in bins
                ],
            )

    def add_correction(self, row: CorrectionRow) -> None:
        """Store the factors published at one moment."""
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO correction (ts, level_factor, fog_applied, fog_shadow,"
                " k_morning, k_recent, fog_reason, today_kwh, remaining_kwh, tomorrow_kwh)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _ts(row.ts),
                    row.level_factor,
                    row.fog_applied,
                    row.fog_shadow,
                    row.k_morning,
                    row.k_recent,
                    row.fog_reason,
                    row.today_kwh,
                    row.remaining_kwh,
                    row.tomorrow_kwh,
                ),
            )

    def add_day(self, report: DayReport | None, stats: DayStats | None) -> None:
        """Store the result of a finished day."""
        day = report.day if report is not None else stats.day if stats is not None else None
        if day is None:
            return
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO day_report (day, report, stats) VALUES (?, ?, ?)",
                (
                    day.isoformat(),
                    json.dumps(None if report is None else report.to_dict()),
                    json.dumps(None if stats is None else stats.to_dict()),
                ),
            )

    def prune(self, before: datetime) -> None:
        """Delete everything older than ``before``."""
        cutoff = _ts(before)
        with self._transaction() as conn:
            conn.execute("DELETE FROM base_snapshot WHERE captured_at < ?", (cutoff,))
            conn.execute("DELETE FROM weather_snapshot WHERE captured_at < ?", (cutoff,))
            conn.execute("DELETE FROM actual WHERE bin_start < ?", (cutoff,))
            conn.execute("DELETE FROM correction WHERE ts < ?", (cutoff,))
            conn.execute(
                "DELETE FROM day_report WHERE day < ?",
                (as_utc(before).date().isoformat(),),
            )

    def clear(self) -> None:
        """Delete all archived data."""
        with self._transaction() as conn:
            for table in (
                "base_value",
                "base_snapshot",
                "weather_value",
                "weather_snapshot",
                "actual",
                "correction",
                "day_report",
            ):
                conn.execute(f"DELETE FROM {table}")

    # ------------------------------------------------------------------ reading

    def snapshots(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> list[Snapshot]:
        """Snapshots captured in ``[start, end)``, oldest first."""
        lo = _ts(start) if start else 0
        hi = _ts(end) if end else 2**62
        heads = self._query(
            "SELECT id, captured_at, refreshed FROM base_snapshot"
            " WHERE captured_at >= ? AND captured_at < ? ORDER BY captured_at, id",
            (lo, hi),
        )
        values: dict[int, list[ForecastPoint]] = {}
        for snapshot_id, period_start, p50, p10, p90 in self._query(
            "SELECT v.snapshot_id, v.period_start, v.p50, v.p10, v.p90 FROM base_value v"
            " JOIN base_snapshot s ON s.id = v.snapshot_id"
            " WHERE s.captured_at >= ? AND s.captured_at < ?",
            (lo, hi),
        ):
            values.setdefault(snapshot_id, []).append(
                ForecastPoint(_dt(period_start), p50, p10, p90)
            )
        return [
            Snapshot(_dt(captured), Forecast.from_points(values.get(sid, [])), bool(refreshed))
            for sid, captured, refreshed in heads
        ]

    def weather_snapshots(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> list[WeatherSnapshot]:
        """Weather runs captured in ``[start, end)``, oldest first."""
        lo = _ts(start) if start else 0
        hi = _ts(end) if end else 2**62
        heads = self._query(
            "SELECT id, captured_at, issued_at, station FROM weather_snapshot"
            " WHERE captured_at >= ? AND captured_at < ? ORDER BY captured_at, id",
            (lo, hi),
        )
        values: dict[int, list[WeatherPoint]] = {}
        for snapshot_id, hour, *rest, extra in self._query(
            "SELECT v.snapshot_id, v.hour_start, v.irradiance, v.relative, v.sunshine, v.fog,"
            " v.low_cloud, v.extra FROM weather_value v"
            " JOIN weather_snapshot s ON s.id = v.snapshot_id"
            " WHERE s.captured_at >= ? AND s.captured_at < ? ORDER BY v.hour_start",
            (lo, hi),
        ):
            values.setdefault(snapshot_id, []).append(
                WeatherPoint(
                    _dt(hour),
                    *rest,
                    extra=tuple(sorted(json.loads(extra).items())) if extra else (),
                )
            )
        return [
            WeatherSnapshot(
                _dt(captured),
                WeatherForecast(station, _dt(issued), tuple(values.get(sid, []))),
            )
            for sid, captured, issued, station in heads
        ]

    def bins(self, start: datetime | None = None, end: datetime | None = None) -> list[MeasuredBin]:
        """Measured bins in ``[start, end)``, oldest first."""
        lo = _ts(start) if start else 0
        hi = _ts(end) if end else 2**62
        return [
            MeasuredBin(_dt(ts), energy, covered, bool(curtailed), gap)
            for ts, energy, covered, curtailed, gap in self._query(
                "SELECT bin_start, energy_kwh, covered_s, curtailed, gap_s FROM actual"
                " WHERE bin_start >= ? AND bin_start < ? ORDER BY bin_start",
                (lo, hi),
            )
        ]

    def day_reports(self) -> list[tuple[date, DayReport | None, DayStats | None]]:
        """Archived day results, oldest first."""
        result = []
        for day, report, stats in self._query(
            "SELECT day, report, stats FROM day_report ORDER BY day"
        ):
            report_data = json.loads(report) if report else None
            stats_data = json.loads(stats) if stats else None
            result.append(
                (
                    date.fromisoformat(day),
                    None if report_data is None else DayReport.from_dict(report_data),
                    None if stats_data is None else DayStats.from_dict(stats_data),
                )
            )
        return result

    def stats(self) -> dict[str, object]:
        """Row counts and time ranges per table (diagnostics)."""
        result: dict[str, object] = {}
        for name, table, column in (
            ("snapshots", "base_snapshot", "captured_at"),
            ("bins", "actual", "bin_start"),
            ("corrections", "correction", "ts"),
            ("weather", "weather_snapshot", "captured_at"),
        ):
            count, first, last = self._query(
                f"SELECT COUNT(*), MIN({column}), MAX({column}) FROM {table}"
            )[0]
            result[name] = {
                "count": count,
                "first": None if first is None else _dt(first).isoformat(),
                "last": None if last is None else _dt(last).isoformat(),
            }
        count, first, last = self._query("SELECT COUNT(*), MIN(day), MAX(day) FROM day_report")[0]
        result["days"] = {"count": count, "first": first, "last": last}
        result["size_bytes"] = self.path.stat().st_size if self.path.exists() else 0
        return result

    def export_csv(self, directory: str | Path) -> list[Path]:
        """Write every table as CSV (ISO timestamps) into ``directory``."""
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        exports = {
            "base_forecast.csv": (
                ["captured_at", "refreshed", "period_start", "p50_kw", "p10_kw", "p90_kw"],
                "SELECT s.captured_at, s.refreshed, v.period_start, v.p50, v.p10, v.p90"
                " FROM base_value v JOIN base_snapshot s ON s.id = v.snapshot_id"
                " ORDER BY s.captured_at, s.id, v.period_start",
                (0, 2),
            ),
            "actual.csv": (
                ["bin_start", "energy_kwh", "covered_s", "curtailed", "gap_s"],
                "SELECT bin_start, energy_kwh, covered_s, curtailed, gap_s FROM actual"
                " ORDER BY bin_start",
                (0,),
            ),
            "correction.csv": (
                [
                    "ts",
                    "level_factor",
                    "fog_applied",
                    "fog_shadow",
                    "k_morning",
                    "k_recent",
                    "fog_reason",
                    "today_kwh",
                    "remaining_kwh",
                    "tomorrow_kwh",
                ],
                "SELECT ts, level_factor, fog_applied, fog_shadow, k_morning, k_recent,"
                " fog_reason, today_kwh, remaining_kwh, tomorrow_kwh FROM correction ORDER BY ts",
                (0,),
            ),
            "weather.csv": (
                [
                    "captured_at",
                    "issued_at",
                    "station",
                    "hour_start",
                    "irradiance_kwh_m2",
                    "relative_pct",
                    "sunshine_h",
                    "fog_pct",
                    "low_cloud_pct",
                    "further_elements",
                ],
                "SELECT s.captured_at, s.issued_at, s.station, v.hour_start, v.irradiance,"
                " v.relative, v.sunshine, v.fog, v.low_cloud, v.extra FROM weather_value v"
                " JOIN weather_snapshot s ON s.id = v.snapshot_id"
                " ORDER BY s.captured_at, s.id, v.hour_start",
                (0, 1, 3),
            ),
            "day_report.csv": (
                ["day", "report", "stats"],
                "SELECT day, report, stats FROM day_report ORDER BY day",
                (),
            ),
        }
        written = []
        for name, (header, sql, time_columns) in exports.items():
            path = target / name
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(header)
                for row in self._query(sql):
                    values = list(row)
                    for index in time_columns:
                        values[index] = _dt(values[index]).isoformat()
                    writer.writerow(values)
            written.append(path)
        return written
