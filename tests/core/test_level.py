"""Layer A: day statistics and the shrunk level factor."""

from datetime import date, timedelta

import pytest

from pvfc_core.actuals import IntervalActual
from pvfc_core.level import DayStats, LevelSettings, compute_level_factor, day_stats

TODAY = date(2026, 11, 20)


def stats(day: date, actual: float, forecast: float, *, missing: int = 0) -> DayStats:
    return DayStats(day, actual, forecast, intervals=20, curtailed=0, missing=missing)


def test_day_stats_skips_night_and_excludes_curtailed_and_missing():
    measured = IntervalActual(1.0, 1.0, False)
    pairs = [
        (0.0, IntervalActual(0.0, 1.0, False)),  # night
        (2.0, measured),
        (2.0, IntervalActual(1.0, 1.0, True)),  # curtailed
        (2.0, IntervalActual(0.2, 0.5, False)),  # not enough readings
        (None, measured),  # no forecast of record
        (0.0, IntervalActual(0.1, 1.0, False)),  # production without forecast
        (0.0, IntervalActual(0.01, 0.0, False, 1.0)),  # night energy from a silent meter
        (None, IntervalActual(0.0, 0.0, False, 0.0)),  # nothing known at all
    ]
    result = day_stats(TODAY, pairs)
    assert result.intervals == 2
    assert result.curtailed == 1
    assert result.missing == 2
    assert result.actual_kwh == pytest.approx(1.1)
    assert result.forecast_kwh == pytest.approx(1.0)


def test_completeness_allows_ten_percent_missing():
    assert DayStats(TODAY, 1, 1, intervals=18, curtailed=0, missing=2).complete
    assert not DayStats(TODAY, 1, 1, intervals=17, curtailed=0, missing=3).complete
    assert not DayStats(TODAY, 0, 0, intervals=0, curtailed=0, missing=0).complete


def test_neutral_without_history():
    level = compute_level_factor([], TODAY, LevelSettings())
    assert level.factor == 1.0
    assert level.days_used == 0


def test_shrinkage_formula():
    history = [stats(TODAY - timedelta(days=d), 7.0, 10.0) for d in range(1, 5)]
    level = compute_level_factor(history, TODAY, LevelSettings())
    # (28 + 2 * 10) / (40 + 2 * 10)
    assert level.factor == pytest.approx(48 / 60)
    assert level.shrink_weight == pytest.approx(20.0)
    assert level.raw_ratio == pytest.approx(0.7)


def test_uses_last_n_complete_days_only():
    history = [stats(TODAY - timedelta(days=d), 5.0, 10.0) for d in range(1, 20)]
    history.append(stats(TODAY - timedelta(days=1), 0.0, 10.0, missing=10))  # incomplete
    history.append(stats(TODAY, 0.0, 10.0))  # today is never used
    level = compute_level_factor(history, TODAY, LevelSettings(days=14))
    assert level.days_used == 14
    assert min(level.days) == TODAY - timedelta(days=14)


def test_ignores_days_older_than_twice_the_window():
    history = [stats(TODAY - timedelta(days=40), 5.0, 10.0)]
    assert compute_level_factor(history, TODAY, LevelSettings()).days_used == 0


def test_factor_is_clamped():
    history = [stats(TODAY - timedelta(days=d), 0.1, 10.0) for d in range(1, 15)]
    level = compute_level_factor(history, TODAY, LevelSettings(shrink_days=0))
    assert level.factor == 0.3
    assert level.clamped


def test_round_trip():
    history = [stats(TODAY - timedelta(days=d), 7.0, 10.0) for d in range(1, 3)]
    level = compute_level_factor(history, TODAY, LevelSettings())
    assert type(level).from_dict(level.to_dict()) == level
    assert DayStats.from_dict(history[0].to_dict()) == history[0]
