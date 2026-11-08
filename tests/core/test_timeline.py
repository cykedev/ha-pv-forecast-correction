"""Time grid, local days and daylight-saving transitions."""

from datetime import UTC, date, datetime, time, timedelta

from pvfc_core.timeline import day_bounds, floor_to, hour_key, local_day, local_time, steps

from .synthetic import TZ


def test_normal_day_has_24_hours():
    start, end = day_bounds(date(2026, 11, 8), TZ)
    assert start == datetime(2026, 11, 7, 23, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=24)


def test_spring_forward_day_has_23_hours():
    start, end = day_bounds(date(2026, 3, 29), TZ)
    assert end - start == timedelta(hours=23)


def test_fall_back_day_has_25_hours():
    start, end = day_bounds(date(2026, 10, 25), TZ)
    assert end - start == timedelta(hours=25)
    assert len(list(steps(start, end, timedelta(minutes=30)))) == 50


def test_local_time_in_spring_gap_moves_forward():
    instant = local_time(date(2026, 3, 29), time(2, 30), TZ)
    assert instant.astimezone(TZ).hour == 3


def test_local_time_ambiguous_takes_first_occurrence():
    instant = local_time(date(2026, 10, 25), time(2, 30), TZ)
    assert instant == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_local_day_uses_time_zone():
    assert local_day(datetime(2026, 11, 7, 23, 30, tzinfo=UTC), TZ) == date(2026, 11, 8)


def test_floor_to_grid():
    value = datetime(2026, 11, 8, 10, 17, 42, tzinfo=UTC)
    assert floor_to(value, timedelta(minutes=5)) == datetime(2026, 11, 8, 10, 15, tzinfo=UTC)


def test_hour_key_separates_repeated_hour():
    first = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # 02:30 CEST
    second = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)  # 02:30 CET
    assert hour_key(first, TZ) == datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
    assert hour_key(second, TZ) == datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
