"""Parsing and writing the Solcast attribute format."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from pvfc_core.forecast_format import (
    Forecast,
    ForecastFormatError,
    ForecastPoint,
    detailed_forecast,
    detailed_hourly,
    energy,
    parse_forecast,
)
from pvfc_core.timeline import day_bounds

from .synthetic import TZ, curve

CET = timezone(timedelta(hours=1))


def test_parses_iso_strings_and_datetimes():
    attributes = {
        "detailedForecast": [
            {
                "period_start": "2026-11-08T12:00:00+01:00",
                "pv_estimate": 3.5,
                "pv_estimate10": 0.6,
                "pv_estimate90": 8.1,
                "dampening_factor": 1.0,
            },
            {
                "period_start": datetime(2026, 11, 8, 12, 30, tzinfo=CET),
                "pv_estimate": "3.25",
                "pv_estimate10": 0.5,
                "pv_estimate90": 7.0,
            },
        ]
    }
    parsed = parse_forecast(attributes)
    assert [p.start for p in parsed.points] == [
        datetime(2026, 11, 8, 11, 0, tzinfo=UTC),
        datetime(2026, 11, 8, 11, 30, tzinfo=UTC),
    ]
    assert parsed.points[1].p50 == 3.25
    assert parsed.has_bands


def test_band_is_optional_and_negative_values_are_clipped():
    parsed = parse_forecast(
        {
            "detailedForecast": [
                {"period_start": "2026-11-08T12:00:00+01:00", "pv_estimate": -0.1},
                {"period_start": "2026-11-08T12:30:00+01:00", "pv_estimate": 1.0},
            ]
        }
    )
    assert parsed.points[0].p50 == 0.0
    assert not parsed.has_bands


def test_hourly_list_is_split_into_half_hours():
    parsed = parse_forecast(
        {
            "detailedForecast": [],
            "detailedHourly": [
                {"period_start": "2026-11-08T12:00:00+01:00", "pv_estimate": 2.0},
                {"period_start": "2026-11-08T13:00:00+01:00", "pv_estimate": 1.0},
            ],
        }
    )
    assert [p.p50 for p in parsed.points] == [2.0, 2.0, 1.0, 1.0]
    assert energy(parsed.points) == pytest.approx(3.0)


@pytest.mark.parametrize(
    "attributes",
    [
        {},
        {"detailedForecast": "not a list"},
        {"detailedForecast": [{"pv_estimate": 1.0}]},
        {"detailedForecast": [{"period_start": "2026-11-08T12:00:00", "pv_estimate": 1}]},
        {"detailedForecast": [{"period_start": "yesterday", "pv_estimate": 1}]},
        {"detailedForecast": [{"period_start": "2026-11-08T12:00:00+01:00", "pv_estimate": None}]},
        {
            "detailedForecast": [
                {"period_start": "2026-11-08T12:00:00+01:00", "pv_estimate": 1},
                {"period_start": "2026-11-08T12:15:00+01:00", "pv_estimate": 1},
            ]
        },
    ],
)
def test_invalid_input_raises(attributes):
    with pytest.raises(ForecastFormatError):
        parse_forecast(attributes)


def test_round_trip_through_attributes():
    points = curve(date(2026, 11, 8))
    attributes = {"detailedForecast": detailed_forecast(points, TZ)}
    reparsed = parse_forecast(attributes)
    assert len(reparsed.points) == 48
    for original, back in zip(points, reparsed.points, strict=True):
        assert back.start == original.start
        assert back.p50 == pytest.approx(original.p50, abs=1e-4)


def test_output_closes_band_where_median_is_zero():
    start = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)
    entries = detailed_forecast([ForecastPoint(start, 0.0, 0.0, 0.4)], TZ)
    assert entries[0]["pv_estimate90"] == 0.0
    assert entries[0]["period_start"].utcoffset() == timedelta(hours=1)


def test_output_without_band_has_no_band_keys():
    start = datetime(2026, 11, 8, 11, 0, tzinfo=UTC)
    entries = detailed_forecast([ForecastPoint(start, 1.0)], TZ)
    assert set(entries[0]) == {"period_start", "pv_estimate"}


def test_hourly_output_on_fall_back_day_has_25_hours():
    day = date(2026, 10, 25)
    points = curve(day)
    hourly = detailed_hourly(points, TZ)
    assert len(hourly) == 25
    assert sum(h["pv_estimate"] for h in hourly) == pytest.approx(energy(points), abs=1e-3)


def test_merge_prefers_first_forecast():
    start, _ = day_bounds(date(2026, 11, 8), TZ)
    first = Forecast.from_points([ForecastPoint(start, 1.0)])
    second = Forecast.from_points(
        [ForecastPoint(start, 2.0), ForecastPoint(start + timedelta(minutes=30), 3.0)]
    )
    merged = first.merged_with(second)
    assert [p.p50 for p in merged.points] == [1.0, 3.0]
