"""The engine end to end: level factor, fog layer, outputs, reports, restarts."""

from datetime import date, datetime, time, timedelta

import pytest

from pvfc_core.archive import Snapshot
from pvfc_core.engine import Corrector, Settings
from pvfc_core.fog import FogGate, FogMode, FogReason, FogSettings
from pvfc_core.forecast_format import Forecast, ForecastPoint, energy
from pvfc_core.replay import replay
from pvfc_core.score import BASE, CORRECTED, SHADOW
from pvfc_core.timeline import BIN, INTERVAL, day_bounds, local_time

from .synthetic import TZ, bins_from, curve, daily_snapshots, day_range, forecast

FIRST = date(2026, 6, 1)
SHADOW_SETTINGS = Settings(fog=FogSettings(mode=FogMode.SHADOW))


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return local_time(day, time(hour, minute), TZ)


def run_days(days, ratio, settings=SHADOW_SETTINGS, **curve_kwargs):
    points = [p for day in days for p in curve(day, **curve_kwargs)]
    return replay(daily_snapshots(days, **curve_kwargs), bins_from(points, ratio), settings, TZ)


def drive(engine: Corrector, start: datetime, end: datetime, bins) -> None:
    """Advance a live engine in 5-minute steps, feeding bins once they ended."""
    pending = sorted(bins, key=lambda b: b.start)
    current = start
    while current <= end:
        while pending and pending[0].start + BIN <= current:
            item = pending.pop(0)
            engine.set_bin(item.start, item.energy_kwh, item.covered_s, item.curtailed)
        engine.update(current)
        current += BIN


def test_level_factor_learns_the_bias_with_shrinkage():
    result = run_days(day_range(FIRST, 16), 0.7)
    by_day = sorted(result.reports.values(), key=lambda r: r.day)
    assert by_day[0].level_factor == 1.0
    for index, report in enumerate(by_day):
        days = min(index, 14)
        assert report.level_factor == pytest.approx((0.7 * days + 2) / (days + 2))


def test_corrected_beats_base_on_biased_days():
    result = run_days(day_range(FIRST, 20), 0.7)
    report = result.reports[FIRST + timedelta(days=18)]
    assert report.complete
    base, corrected = report.variants[BASE], report.variants[CORRECTED]
    assert abs(corrected.rest_mae) < 0.2 * base.rest_mae
    assert abs(corrected.day) < abs(base.day)
    assert abs(corrected.day_ahead) < abs(base.day_ahead)
    assert set(base.rest) == {"09:00", "10:00", "11:00", "12:00", "13:00", "14:00"}


def test_rest_of_day_errors_are_exact():
    result = run_days(day_range(FIRST, 2), 0.5)
    report = result.reports[FIRST]
    base_rest = energy(p for p in curve(FIRST) if p.start >= at(FIRST, 9))
    assert report.actual_rest["09:00"] == pytest.approx(0.5 * base_rest)
    assert report.variants[BASE].rest["09:00"] == pytest.approx(0.5 * base_rest)
    assert report.actual_kwh == pytest.approx(0.5 * energy(curve(FIRST)))


def test_fog_shadow_does_not_change_the_published_forecast():
    days = day_range(FIRST, 2)
    gate = FogSettings(mode=FogMode.SHADOW, gate=FogGate(threshold=0.7, min_confidence=0.5))
    result = run_days(days, 0.3, Settings(fog=gate))
    report = result.reports[FIRST]
    assert report.fog_active
    assert report.variants[CORRECTED].rest == report.variants[BASE].rest
    assert abs(report.variants[SHADOW].rest_mae) < 0.1 * report.variants[BASE].rest_mae


def test_fog_on_scales_the_rest_of_the_day():
    days = day_range(FIRST, 2)
    gate = FogSettings(mode=FogMode.ON, gate=FogGate(threshold=0.7, min_confidence=None))
    report = run_days(days, 0.3, Settings(fog=gate)).reports[FIRST]
    assert SHADOW not in report.variants
    # The rest of the day was forecast exactly: corrected = 0.3 x base = actual.
    assert report.variants[CORRECTED].rest["12:00"] == pytest.approx(0.0, abs=1e-9)
    assert report.variants[BASE].rest["12:00"] == pytest.approx(
        0.7 / 0.3 * report.actual_rest["12:00"]
    )


def test_fog_gate_stays_closed_on_normal_days():
    gate = FogSettings(mode=FogMode.ON, gate=FogGate(threshold=0.7, min_confidence=0.5))
    report = run_days(day_range(FIRST, 2), 0.95, Settings(fog=gate)).reports[FIRST]
    assert not report.fog_active
    assert report.variants[CORRECTED].rest == report.variants[BASE].rest


def test_live_outputs_mix_measured_past_and_corrected_future():
    day = FIRST
    engine = Corrector(Settings(), TZ)
    base = forecast([day, day + timedelta(days=1)])
    start, _ = day_bounds(day, TZ)
    engine.set_base(base, start)
    now = at(day, 12, 10)
    drive(engine, start, now, bins_from(curve(day), 0.5))
    result = engine.result(now)

    assert result.level_applied == 1.0
    today = result.today
    assert today is not None
    morning = next(p for p in today.points if p.start == at(day, 9))
    assert morning.p50 == pytest.approx(0.5 * base.as_dict()[at(day, 9)].p50)
    assert morning.p10 == morning.p50 == morning.p90
    afternoon = next(p for p in today.points if p.start == at(day, 15))
    assert afternoon == base.as_dict()[at(day, 15)]

    remaining = result.remaining
    assert remaining is not None
    assert remaining.points[0].start == at(day, 12)
    current = base.as_dict()[at(day, 12)]
    future = energy(p for p in curve(day) if p.start >= at(day, 12, 30))
    assert remaining.estimate == pytest.approx(future + current.p50 * 0.5 * 20 / 30)
    assert remaining.base_estimate == pytest.approx(remaining.estimate)
    before_noon = energy(p for p in curve(day) if p.start < at(day, 12))
    assert result.actual_so_far == pytest.approx(0.5 * (before_noon + current.p50 * 10 / 60))
    assert result.tomorrow is not None
    assert result.tomorrow.estimate == pytest.approx(energy(curve(day + timedelta(days=1))))


def test_outputs_without_band():
    day = FIRST
    engine = Corrector(Settings(), TZ)
    start, _ = day_bounds(day, TZ)
    engine.set_base(forecast([day], bands=False), start)
    engine.update(at(day, 10))
    result = engine.result(at(day, 10))
    assert result.today is not None
    assert result.today.estimate10 is None
    assert all(p.p10 is None for p in result.today.points)


def test_refresh_detection_ignores_new_days():
    day = FIRST
    engine = Corrector(Settings(), TZ)
    start, _ = day_bounds(day, TZ)
    first = engine.set_base(forecast([day]), start)
    assert first.refreshed
    extended = engine.set_base(forecast([day, day + timedelta(days=1)]), start + INTERVAL)
    assert not extended.refreshed
    assert extended.changed
    assert engine.base_refreshed_at == start
    changed = engine.set_base(forecast([day], peak=5.0), start + 2 * INTERVAL)
    assert changed.refreshed
    assert engine.base_refreshed_at == start + 2 * INTERVAL


def test_unavailable_base_keeps_last_forecast_and_counts_failures():
    day = FIRST
    engine = Corrector(Settings(), TZ)
    start, _ = day_bounds(day, TZ)
    engine.set_base(forecast([day]), start)
    engine.base_unavailable(start + INTERVAL)
    engine.base_unavailable(start + 2 * INTERVAL)
    engine.update(at(day, 9))
    result = engine.result(at(day, 9))
    assert not result.base_valid
    assert result.base_failures_today == 1
    assert result.today is not None
    engine.set_base(Forecast(), start + 3 * INTERVAL)
    assert engine.base_failures_today == 1


def test_forecast_of_record_is_the_value_when_the_interval_began():
    day = FIRST
    engine = Corrector(Settings(), TZ)
    start, _ = day_bounds(day, TZ)
    engine.set_base(forecast([day]), start)
    engine.update(at(day, 9))
    engine.set_base(forecast([day], peak=8.0), at(day, 9, 5))
    engine.update(at(day, 9, 30))
    assert engine.record[at(day, 9)] == forecast([day]).as_dict()[at(day, 9)]
    assert engine.record[at(day, 9, 30)] == forecast([day], peak=8.0).as_dict()[at(day, 9, 30)]


def test_restart_restores_the_full_state():
    day = FIRST
    settings = Settings(fog=FogSettings(mode=FogMode.ON, gate=FogGate(0.7, None)))
    engine = Corrector(settings, TZ)
    start, _ = day_bounds(day, TZ)
    engine.set_base(forecast([day, day + timedelta(days=1)]), start)
    drive(engine, start, at(day, 11), bins_from(curve(day), 0.3))
    engine.observe_energy(at(day, 11), 12.5)
    restored = Corrector.from_dict(engine.to_dict(), settings, TZ)
    assert restored.result(at(day, 11)) == engine.result(at(day, 11))
    assert restored.fog.reason is FogReason.ACTIVE


def test_unknown_storage_version_starts_fresh():
    engine = Corrector.from_dict({"version": 99, "day": "2026-01-01"}, Settings(), TZ)
    assert engine.day is None


def test_curtailed_intervals_do_not_teach_the_level_factor():
    days = day_range(FIRST, 4)
    curtailed = {p.start for day in days for p in curve(day) if p.p50 > 2.0}
    points = [p for day in days for p in curve(day)]

    def ratio(start: datetime) -> float:
        return 0.5 if start in curtailed else 1.0

    result = replay(
        daily_snapshots(days),
        bins_from(points, ratio, curtailed=curtailed),
        Settings(),
        TZ,
    )
    last = result.reports[days[-1]]
    assert last.level_factor == pytest.approx(1.0)
    stats = result.stats[days[0]]
    assert stats.curtailed > 0


def test_daylight_saving_days_are_reported():
    days = [date(2026, 3, 28), date(2026, 3, 29), date(2026, 10, 24), date(2026, 10, 25)]
    for first in (days[0], days[2]):
        pair = [first, first + timedelta(days=1)]
        result = run_days(pair, 0.8)
        for day in pair:
            report = result.reports[day]
            assert report.complete
            assert report.actual_kwh == pytest.approx(0.8 * energy(curve(day)))


def test_missing_measurements_make_the_day_incomplete():
    days = day_range(FIRST, 2)
    points = [p for day in days for p in curve(day)]
    bins = [b for b in bins_from(points, 1.0) if not at(days[0], 10) <= b.start < at(days[0], 13)]
    result = replay(daily_snapshots(days), bins, Settings(), TZ)
    assert not result.reports[days[0]].complete
    assert result.reports[days[1]].complete


def test_level_disabled_reports_but_does_not_apply():
    result = run_days(day_range(FIRST, 5), 0.6, Settings(level_enabled=False))
    report = result.reports[FIRST + timedelta(days=4)]
    assert report.level_factor == 1.0
    assert report.variants[CORRECTED].rest == report.variants[BASE].rest


def test_points_are_kw_means():
    point = ForecastPoint(at(FIRST, 12), 2.0, 1.0, 3.0)
    assert energy([point]) == pytest.approx(1.0)


def test_energy_from_an_outage_is_not_counted_twice():
    day = FIRST
    engine = Corrector(Settings(), TZ)
    start, _ = day_bounds(day, TZ)
    engine.set_base(forecast([day, day + timedelta(days=1)]), start)
    produced = {p.start: p.p50 * 0.5 for p in curve(day)}

    def meter_at(when: datetime) -> float:
        return sum(
            e * min(1.0, max(0.0, (when - s).total_seconds() / 1800)) for s, e in produced.items()
        )

    for minute in range(0, 9 * 60 + 1, 5):  # readings until 09:00
        when = start + timedelta(minutes=minute)
        engine.observe_energy(when, meter_at(when))
        engine.update(when)
    noon = at(day, 12)
    engine.observe_energy(noon, meter_at(noon))  # first reading after a 3-hour outage
    engine.update(noon)
    result = engine.result(noon)
    assert result.actual_so_far == pytest.approx(meter_at(noon))
    assert result.today.estimate == pytest.approx(energy(curve(day)), rel=1e-6)


def test_a_silent_meter_at_night_keeps_days_complete():
    days = day_range(FIRST, 3)
    points = [p for day in days for p in curve(day)]
    bins = []
    for item in bins_from(points, 0.8):
        local_hour = item.start.astimezone(TZ).hour
        if 7 <= local_hour < 17:
            bins.append(item)
        else:  # the meter is silent at night: only energy spread from the gap
            bins.append(type(item)(item.start, 0.0, 0.0, False, 300.0))
    result = replay(daily_snapshots(days), bins, Settings(), TZ)
    assert all(report.complete for report in result.reports.values())
    assert result.reports[days[-1]].level_factor < 1.0


def test_switching_the_meter_input_forgets_the_last_reading():
    engine = Corrector(Settings(), TZ)
    engine.use_meter_source("energy:sensor.daily")
    engine.observe_energy(at(FIRST, 10), 4.2)
    engine.use_meter_source("energy:sensor.lifetime")
    engine.observe_energy(at(FIRST, 10, 5), 15321.0)
    engine.observe_energy(at(FIRST, 10, 10), 15321.5)
    assert engine.bins.actual(at(FIRST, 10), at(FIRST, 10, 10)).energy_kwh == pytest.approx(0.5)
    restored = Corrector.from_dict(engine.to_dict(), Settings(), TZ)
    assert restored.meter_source == "energy:sensor.lifetime"


def test_implausible_readings_are_rejected_using_the_forecast_peak():
    engine = Corrector(Settings(), TZ)
    start, _ = day_bounds(FIRST, TZ)
    engine.set_base(forecast([FIRST]), start)
    engine.observe_energy(at(FIRST, 10), 5.0)
    engine.observe_energy(at(FIRST, 10, 5), 0.0)
    engine.observe_energy(at(FIRST, 10, 10), 5.2)
    assert engine.rejected_readings == 1
    assert engine.bins.actual(at(FIRST, 10), at(FIRST, 10, 15)).energy_kwh == 0.0


def test_replay_updates_when_a_snapshot_arrives_like_live():
    day = FIRST
    start, _ = day_bounds(day, TZ)
    days = [day, day + timedelta(days=1)]
    refreshed_at = at(day, 10) + timedelta(seconds=2)
    snapshots = [
        Snapshot(start, forecast(days), True),
        Snapshot(refreshed_at, forecast(days, peak=8.0), True),
    ]
    result = replay(snapshots, bins_from(curve(day), 1.0), Settings(), TZ)
    old = {p.start: p.p50 for p in curve(day)}
    new = {p.start: p.p50 for p in curve(day, peak=8.0)}
    # Live, the refresh at 10:00:02 is seen before the 10:00:05 timer: the 10:00 interval
    # is recorded with the refreshed value.
    expected = sum((old[s] if s < at(day, 10) else new[s]) * 0.5 for s in old if old[s] > 0)
    assert result.stats[day].forecast_kwh == pytest.approx(expected)


def test_expects_production_follows_the_base():
    engine = Corrector(Settings(), TZ)
    assert engine.expects_production(at(FIRST, 2))  # unknown without a base
    start, _ = day_bounds(FIRST, TZ)
    engine.set_base(forecast([FIRST]), start)
    assert not engine.expects_production(at(FIRST, 2))
    assert engine.expects_production(at(FIRST, 12))
