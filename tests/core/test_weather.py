"""Layer C: reading DWD MOSMIX, the station catalogue, the weather factor and its use."""

import io
import zipfile
from datetime import UTC, date, datetime, timedelta

import pytest

from pvfc_core.archive import Archive, WeatherSnapshot
from pvfc_core.engine import Corrector, Settings
from pvfc_core.forecast_format import energy
from pvfc_core.level import DayStats
from pvfc_core.replay import replay
from pvfc_core.score import CORRECTED, WEATHER
from pvfc_core.timeline import INTERVAL_HOURS, day_bounds, floor_to
from pvfc_core.weather import (
    HOUR,
    WeatherError,
    WeatherForecast,
    WeatherMode,
    WeatherPoint,
    WeatherSettings,
    hour_factor,
    nearest_station,
    parse_mosmix,
    parse_stations,
    typical_ratio,
)

from .synthetic import TZ, bins_from, curve, daily_snapshots, day_range, forecast

FIRST = date(2026, 6, 1)


def kml(issued: str, steps: list[str], elements: dict[str, list[str]]) -> bytes:
    forecasts = "".join(
        f'<dwd:Forecast dwd:elementName="{name}">\n<dwd:value>     {" ".join(values)}</dwd:value>'
        "\n</dwd:Forecast>"
        for name, values in elements.items()
    )
    return (
        '<?xml version="1.0" encoding="ISO-8859-1"?><kml:kml><kml:Document><kml:ExtendedData>'
        f"<dwd:ProductDefinition><dwd:IssueTime>{issued}</dwd:IssueTime>"
        "<dwd:ForecastTimeSteps>"
        + "".join(f"<dwd:TimeStep>{s}</dwd:TimeStep>" for s in steps)
        + "</dwd:ForecastTimeSteps></dwd:ProductDefinition></kml:ExtendedData>"
        f"<kml:Placemark><kml:ExtendedData>{forecasts}</kml:ExtendedData></kml:Placemark>"
        "</kml:Document></kml:kml>"
    ).encode("latin-1")


def kmz(document: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("MOSMIX_L_2026110815_X1234.kml", document)
    return buffer.getvalue()


def test_reads_a_mosmix_document_kmz_or_kml():
    document = kml(
        "2026-11-08T15:00:00.000Z",
        ["2026-11-09T08:00:00.000Z", "2026-11-09T09:00:00.000Z"],
        {
            "Rad1h": ["180.00", "720.00"],
            "RRad1": ["20.0", "-"],
            "SunD1": ["0.0", "1800.0"],
            "wwM": ["60.0", "10.0"],
            "Nl": ["90.0", "40.0"],
        },
    )
    for content in (document, kmz(document)):
        result = parse_mosmix(content, "X1234")
        assert result.issued_at == datetime(2026, 11, 8, 15, tzinfo=UTC)
        first, second = result.points
        assert first.start == datetime(2026, 11, 9, 7, tzinfo=UTC)  # the hour before the step
        assert first.irradiance == pytest.approx(0.05)
        assert second.irradiance == pytest.approx(0.2)
        assert second.relative is None
        assert second.sunshine == pytest.approx(0.5)
        assert first.fog == 60.0
        assert first.low_cloud == 90.0


def test_rejects_documents_without_irradiance_or_times():
    with pytest.raises(WeatherError):
        parse_mosmix(kml("2026-11-08T15:00:00Z", ["2026-11-09T08:00:00Z"], {"wwM": ["5"]}), "X")
    with pytest.raises(WeatherError):
        parse_mosmix(b"<kml/>", "X")
    with pytest.raises(WeatherError):
        parse_mosmix(b"PK\x03\x04broken", "X")


def test_finds_the_nearest_station():
    catalogue = (
        "ID    ICAO NAME                 LAT    LON     ELEV\n"
        "----- ---- -------------------- -----  ------- -----\n"
        "10865 ---- MUENCHEN STADT        48.10   11.32   515\n"
        "10870 EDDM MUENCHEN/FLUGHAFEN    48.21   11.47   446\n"
        " Z949 ---- JENNER                47.35   13.01  1200\n"
    )
    stations = parse_stations(catalogue)
    assert [s.id for s in stations] == ["10865", "10870", "Z949"]
    assert stations[0].latitude == pytest.approx(48 + 10 / 60)
    best = nearest_station(stations, 48.14, 11.58)
    assert best.id == "10865"
    assert best.distance_km == pytest.approx(5, abs=2)


def test_the_factor_follows_the_disagreement_with_the_usual_ratio():
    settings = WeatherSettings(trust=1.0, min_factor=0.3, max_factor=1.5)
    typical = 0.1  # kWh/m² per kWh of base forecast, as usual
    assert hour_factor(0.1, 1.0, typical, settings) == pytest.approx(1.0)
    assert hour_factor(0.05, 1.0, typical, settings) == pytest.approx(0.5)
    assert hour_factor(0.01, 1.0, typical, settings) == 0.3  # clamped
    assert hour_factor(0.12, 1.0, typical, settings) == pytest.approx(1.2)
    assert hour_factor(0.3, 1.0, typical, settings) == 1.5  # clamped
    lower_only = WeatherSettings(trust=1.0, max_factor=1.0)
    assert hour_factor(0.2, 1.0, typical, lower_only) == 1.0
    half = WeatherSettings(trust=0.5)
    assert hour_factor(0.025, 1.0, typical, half) == pytest.approx(0.5)  # sqrt(0.25)
    assert hour_factor(None, 1.0, typical, settings) == 1.0
    assert hour_factor(0.05, 0.0, typical, settings) == 1.0


def test_typical_ratio_draws_hours_toward_the_whole_day():
    days = [
        (FIRST + timedelta(days=d), ((9, 0.2, 1.0), (12, 0.4, 2.0), (15, 0.3, 1.0)))
        for d in range(5)
    ]
    typical = typical_ratio(days, WeatherSettings())
    assert typical.days_used == 5
    assert typical.ratio == pytest.approx(0.9 / 4.0)
    assert typical.at(12) == pytest.approx(0.2, abs=0.02)
    assert 0.225 < typical.at(15) < 0.3  # pulled from 0.3 toward 0.225
    assert typical.at(3) == typical.ratio


def weather_runs(days: list[date], ratio=lambda day: 1.0, *, scale: float = 0.1):
    """One run per day at 03:00 UTC: irradiance proportional to the base forecast
    times ``ratio(day)``, for that day and the next."""
    runs = []
    for day in days:
        points = []
        for target in (day, day + timedelta(days=1)):
            hourly: dict[datetime, float] = {}
            for p in curve(target):
                hour = floor_to(p.start, HOUR)
                hourly[hour] = hourly.get(hour, 0.0) + p.p50 * INTERVAL_HOURS
            for hour, kwh in sorted(hourly.items()):
                points.append(WeatherPoint(hour, kwh * scale * ratio(target), 50.0, 0.5, 5.0, 30.0))
        start, _ = day_bounds(day, TZ)
        issued = start + timedelta(hours=3)
        runs.append(WeatherSnapshot(issued, WeatherForecast("X1234", issued, tuple(points))))
    return runs


SHADOW = Settings(weather=WeatherSettings(mode=WeatherMode.SHADOW, trust=1.0))


def test_shadow_weather_is_scored_and_beats_the_correction_on_a_dull_day():
    days = day_range(FIRST, 12)
    dull = FIRST + timedelta(days=10)
    ratio = lambda d: 0.4 if d == dull else 1.0  # noqa: E731
    points = [p for day in days for p in curve(day)]
    result = replay(
        daily_snapshots(days),
        bins_from(points, lambda start: 0.4 if start.astimezone(TZ).date() == dull else 1.0),
        SHADOW,
        TZ,
        weather=weather_runs(days, ratio),
    )
    report = result.reports[dull]
    weather, corrected = report.variants[WEATHER], report.variants[CORRECTED]
    assert abs(weather.day_ahead) < 0.2 * abs(corrected.day_ahead)
    assert weather.rest_mae < 0.2 * corrected.rest_mae
    normal = result.reports[FIRST + timedelta(days=8)]
    assert normal.variants[WEATHER].rest_mae == pytest.approx(
        normal.variants[CORRECTED].rest_mae, abs=0.05
    )
    assert result.stats[FIRST + timedelta(days=8)].weather  # pairs are learned per day


def run_live(settings: Settings, ratio: float):
    engine = Corrector(settings, TZ)
    engine.set_base(forecast([FIRST, FIRST + timedelta(days=1)]), day_bounds(FIRST, TZ)[0])
    [run] = weather_runs([FIRST], lambda d: ratio)
    engine.set_weather(run.forecast, run.captured_at)
    now = day_bounds(FIRST, TZ)[0] + timedelta(hours=6)
    engine.update(now)
    return engine, engine.result(now)


def test_shadow_does_not_change_the_published_forecast_but_on_does():
    _, plain = run_live(Settings(), 1.0)
    engine, shadow = run_live(SHADOW, 0.5)
    assert shadow.tomorrow.estimate == pytest.approx(plain.tomorrow.estimate)
    # From the first day: today and tomorrow are both dull, so they set the usual ratio
    # themselves and nothing stands out.
    assert shadow.weather.rest_factor == pytest.approx(1.0)
    assert not shadow.weather.learned
    on = Settings(weather=WeatherSettings(mode=WeatherMode.ON, trust=1.0))
    engine, _ = run_live(on, 1.0)
    engine.weather_typical = typical_ratio(
        [(FIRST - timedelta(days=1), ((h, 0.2, 1.0) for h in range(24)))], on.weather
    )
    engine.update(day_bounds(FIRST, TZ)[0] + timedelta(hours=6, minutes=5))
    result = engine.result(day_bounds(FIRST, TZ)[0] + timedelta(hours=6, minutes=5))
    # MOSMIX sees half the usual irradiance per base kWh: the forecast is halved.
    assert result.weather.tomorrow_factor == pytest.approx(0.5, abs=0.02)
    assert result.tomorrow.estimate == pytest.approx(0.5 * plain.tomorrow.estimate, rel=0.05)
    assert result.weather.hours_tomorrow


def test_weather_state_survives_a_restart():
    engine, _ = run_live(SHADOW, 0.8)
    engine.history[FIRST - timedelta(days=1)] = DayStats(
        FIRST - timedelta(days=1), 10.0, 10.0, 18, 0, 0, weather=((10, 0.3, 2.0),)
    )
    engine.recompute_level(day_bounds(FIRST, TZ)[0] + timedelta(hours=6))
    restored = Corrector.from_dict(engine.to_dict(), SHADOW, TZ)
    assert restored.weather == engine.weather
    assert restored.weather_typical == engine.weather_typical
    assert restored.weather_typical.days_used == 1
    assert restored.weather_record == engine.weather_record
    assert restored.history == engine.history


def test_weather_runs_are_archived(tmp_path):
    archive = Archive(tmp_path / "a.db")
    archive.open()
    [run] = weather_runs([FIRST])
    archive.add_weather(run.captured_at, run.forecast)
    [back] = archive.weather_snapshots()
    assert back.captured_at == run.captured_at
    assert back.forecast.station == "X1234"
    assert back.forecast.points == run.forecast.points
    assert archive.stats()["weather"]["count"] == 1
    assert energy(curve(FIRST)) > 0
    archive.close()
