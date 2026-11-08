"""Measured energy per bin: energy and power sensors, gaps, resets, curtailment."""

from datetime import UTC, datetime, timedelta

import pytest

from pvfc_core.actuals import BinStore, CurtailmentTracker, EnergyMeter, PowerMeter

T0 = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)


def minutes(value: float) -> datetime:
    return T0 + timedelta(minutes=value)


def test_energy_meter_spreads_delta_over_bins():
    bins = BinStore()
    meter = EnergyMeter(bins)
    meter.observe(minutes(0), 10.0)
    meter.observe(minutes(10), 11.0)
    first = bins.actual(minutes(0), minutes(5))
    assert first.energy_kwh == pytest.approx(0.5)
    assert first.coverage == pytest.approx(1.0)
    assert bins.actual(minutes(0), minutes(10)).energy_kwh == pytest.approx(1.0)


def test_energy_meter_handles_daily_reset_and_jitter():
    bins = BinStore()
    meter = EnergyMeter(bins)
    meter.observe(minutes(0), 20.0)
    meter.observe(minutes(5), 19.99)  # jitter: ignored, baseline stays 20.0
    meter.observe(minutes(10), 20.5)
    meter.observe(minutes(15), 0.2)  # reset: 0.2 kWh since the reset
    total = bins.actual(minutes(0), minutes(15)).energy_kwh
    assert total == pytest.approx(0.7)


def test_energy_meter_long_gap_keeps_energy_but_not_coverage():
    bins = BinStore()
    meter = EnergyMeter(bins, max_gap=timedelta(minutes=30))
    meter.observe(minutes(0), 1.0)
    meter.observe(minutes(60), 3.0)
    measured = bins.actual(minutes(0), minutes(60))
    assert measured.energy_kwh == pytest.approx(2.0)
    assert measured.coverage == 0.0
    assert not measured.measured


def test_energy_meter_attributes_energy_to_the_reporting_period():
    bins = BinStore()
    meter = EnergyMeter(bins)
    meter.observe(minutes(0), 1.0)
    meter.observe(minutes(5), 1.0)  # repeated tick without a new report: ignored below
    meter.observe(minutes(5), 1.0)
    meter.observe(minutes(15), 1.6)  # the meter reports every 15 minutes
    assert bins.actual(minutes(5), minutes(15)).energy_kwh == pytest.approx(0.6)
    assert bins.actual(minutes(0), minutes(5)).energy_kwh == 0.0


def test_energy_meter_skips_unavailable_readings():
    bins = BinStore()
    meter = EnergyMeter(bins)
    meter.observe(minutes(0), 1.0)
    meter.observe(minutes(5), None)
    meter.observe(minutes(10), 1.5)
    assert bins.actual(minutes(0), minutes(10)).energy_kwh == pytest.approx(0.5)


def test_power_meter_integrates_held_values():
    bins = BinStore()
    meter = PowerMeter(bins)
    meter.observe(minutes(0), 6.0)
    meter.observe(minutes(10), 3.0)
    meter.observe(minutes(20), 3.0)
    assert bins.actual(minutes(0), minutes(10)).energy_kwh == pytest.approx(1.0)
    assert bins.actual(minutes(10), minutes(20)).energy_kwh == pytest.approx(0.5)


def test_power_meter_leaves_long_gap_uncovered():
    bins = BinStore()
    meter = PowerMeter(bins, max_hold=timedelta(minutes=20))
    meter.observe(minutes(0), 6.0)
    meter.observe(minutes(60), 6.0)
    measured = bins.actual(minutes(0), minutes(60))
    assert measured.energy_kwh == 0.0
    assert measured.coverage == 0.0


def test_power_meter_stops_at_unavailable():
    bins = BinStore()
    meter = PowerMeter(bins)
    meter.observe(minutes(0), 6.0)
    meter.observe(minutes(5), None)
    meter.observe(minutes(15), 6.0)
    measured = bins.actual(minutes(0), minutes(15))
    assert measured.energy_kwh == pytest.approx(0.5)
    assert measured.coverage == pytest.approx(1 / 3)


def test_power_meter_takes_late_readings_as_simultaneous():
    bins = BinStore()
    meter = PowerMeter(bins)
    meter.observe(minutes(0), 6.0)
    meter.observe(minutes(10), 6.0)
    meter.observe(minutes(10) - timedelta(seconds=1), None)  # slightly out of order
    meter.observe(minutes(20), None)
    assert bins.actual(minutes(0), minutes(20)).energy_kwh == pytest.approx(1.0)


def test_curtailment_marks_bins_while_on():
    bins = BinStore()
    tracker = CurtailmentTracker(bins)
    tracker.observe(minutes(0), False)
    tracker.observe(minutes(12), True)
    tracker.observe(minutes(17), False)
    assert not bins.actual(minutes(0), minutes(10)).curtailed
    assert bins.actual(minutes(10), minutes(15)).curtailed
    assert bins.actual(minutes(15), minutes(20)).curtailed
    assert not bins.actual(minutes(20), minutes(25)).curtailed


def test_bins_round_trip_and_prune():
    bins = BinStore()
    bins.add_energy(minutes(0), minutes(30), 1.5, covered=True)
    bins.mark_curtailed(minutes(20), minutes(20))
    restored = BinStore.from_dict(bins.to_dict())
    assert restored.interval(minutes(0)) == bins.interval(minutes(0))
    restored.prune(minutes(15))
    assert restored.actual(minutes(0), minutes(15)).energy_kwh == 0.0
    assert restored.actual(minutes(15), minutes(30)).energy_kwh == pytest.approx(0.75)


def test_meters_restore_last_reading():
    bins = BinStore()
    meter = EnergyMeter(bins)
    meter.observe(minutes(0), 5.0)
    other = EnergyMeter(bins)
    other.restore(meter.to_dict())
    other.observe(minutes(5), 5.5)
    assert bins.actual(minutes(0), minutes(5)).energy_kwh == pytest.approx(0.5)


def test_lifetime_meter_glitches_do_not_create_energy():
    bins = BinStore()
    meter = EnergyMeter(bins, max_power_kw=20.0)
    meter.observe(minutes(0), 12345.6)
    meter.observe(minutes(5), 12345.5)  # correction: not a reset
    meter.observe(minutes(10), 12345.7)
    assert bins.actual(minutes(0), minutes(10)).energy_kwh == pytest.approx(0.1)
    meter.observe(minutes(15), 0.0)  # transient zero
    meter.observe(minutes(20), 12345.8)  # back: would mean 148 MW
    assert bins.actual(minutes(0), minutes(25)).energy_kwh == pytest.approx(0.1)
    assert meter.rejected == 1
    meter.observe(minutes(25), 12345.9)
    assert bins.actual(minutes(0), minutes(25)).energy_kwh == pytest.approx(0.2)


def test_energy_from_a_long_gap_is_known_but_not_covered():
    bins = BinStore()
    meter = EnergyMeter(bins, max_gap=timedelta(hours=2))
    meter.observe(minutes(0), 1.0)
    meter.observe(minutes(180), 4.0)
    measured = bins.actual(minutes(0), minutes(180))
    assert measured.energy_kwh == pytest.approx(3.0)
    assert measured.coverage == 0.0
    assert measured.known == pytest.approx(1.0)
    restored = BinStore.from_dict(bins.to_dict())
    assert restored.actual(minutes(0), minutes(180)) == measured
