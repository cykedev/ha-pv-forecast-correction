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
    # (28 + 1 * 10) / (40 + 1 * 10)
    assert level.factor == pytest.approx(38 / 50)
    assert level.shrink_weight == pytest.approx(10.0)
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


def hourly(day: date, morning: float, midday: float, *, dawn: float = 0.3) -> DayStats:
    """A day whose morning hours (8-10) run at ``morning`` and midday (11-15) at ``midday``
    of the forecast; one weak hour at dawn (7) runs at ``dawn``."""
    hours = [(7, 0.1 * dawn, 0.1)]
    hours += [(h, 2.0 * morning, 2.0) for h in (8, 9, 10)]
    hours += [(h, 3.0 * midday, 3.0) for h in (11, 12, 13, 14, 15)]
    actual = sum(a for _, a, _ in hours)
    forecast = sum(f for _, _, f in hours)
    return DayStats(day, actual, forecast, 18, 0, 0, hours=tuple(hours))


def test_time_curve_follows_the_hours_and_shrinks_weak_ones():
    history = [hourly(TODAY - timedelta(days=d), 0.4, 0.8) for d in range(1, 15)]
    level = compute_level_factor(history, TODAY, LevelSettings(by_time=True))
    curve = dict(level.curve)
    assert curve[9] < level.factor < curve[12]
    # Two pseudo-days pull each hour a little toward the day factor.
    assert curve[12] / curve[9] == pytest.approx(0.8 / 0.4, rel=0.15)
    # The curve only shapes the day: weighted with each hour's forecast it is the factor.
    weights = {h: sum(f for d in history for hh, _, f in d.hours if hh == h) for h in curve}
    mean = sum(curve[h] * w for h, w in weights.items()) / sum(weights.values())
    assert mean == pytest.approx(level.factor, rel=1e-6)
    # The dawn hour carries almost no energy: it stays near the day factor, not at 0.3.
    assert abs(curve[7] - level.factor) < abs(0.3 - level.factor) / 2
    assert level.at(9) == curve[9]
    assert level.at(3) == level.factor  # hours without data use the day factor


def test_days_without_hours_do_not_take_over_the_level():
    """Imported days known only by their sums set the level with the rest; the one day
    with hours only shapes it."""
    history = [DayStats.imported_sums(TODAY - timedelta(days=d), 7.0, 10.0) for d in range(2, 15)]
    history.append(hourly(TODAY - timedelta(days=1), 0.3, 0.3, dawn=0.3))
    level = compute_level_factor(history, TODAY, LevelSettings(by_time=True))
    weights = {h: f for h, _, f in history[-1].hours}
    curve = dict(level.curve)
    mean = sum(curve[h] * w for h, w in weights.items()) / sum(weights.values())
    assert mean == pytest.approx(level.factor, rel=1e-6)
    assert level.factor > 0.65


def test_time_curve_off_or_without_hourly_data():
    history = [hourly(TODAY - timedelta(days=d), 0.4, 0.8) for d in range(1, 5)]
    assert compute_level_factor(history, TODAY, LevelSettings(by_time=False)).curve == ()
    imported = [DayStats.imported_sums(TODAY - timedelta(days=d), 7.0, 10.0) for d in range(1, 5)]
    level = compute_level_factor(imported, TODAY, LevelSettings(by_time=True))
    assert level.curve == ()
    assert level.at(9) == level.factor


def test_day_stats_keep_hourly_sums_and_round_trip():
    measured = IntervalActual(1.0, 1.0, False)
    result = day_stats(TODAY, [(2.0, measured, 9), (2.0, measured, 9), (4.0, measured, 12)])
    assert result.hours == ((9, 2.0, 2.0), (12, 1.0, 2.0))
    assert DayStats.from_dict(result.to_dict()) == result
    history = [hourly(TODAY - timedelta(days=d), 0.4, 0.8) for d in range(1, 4)]
    level = compute_level_factor(history, TODAY, LevelSettings(by_time=True))
    assert type(level).from_dict(level.to_dict()) == level
