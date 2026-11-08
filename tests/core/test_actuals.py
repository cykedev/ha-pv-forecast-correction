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


def test_energy_meter_spreads_a_step_since_the_last_change():
    bins = BinStore()
    meter = EnergyMeter(bins)
    meter.observe(minutes(0), 1.0)
    meter.observe(minutes(5), 1.0)  # the same value repeated: ignored
    meter.observe(minutes(10), 1.0)
    meter.observe(minutes(15), 1.6)  # the counter steps every 15 minutes
    assert bins.actual(minutes(0), minutes(5)).energy_kwh == pytest.approx(0.2)
    assert bins.actual(minutes(0), minutes(15)).energy_kwh == pytest.approx(0.6)
    assert bins.actual(minutes(0), minutes(15)).coverage == pytest.approx(1.0)


def test_a_confirmed_standstill_is_measured_zero():
    bins = BinStore()
    meter = EnergyMeter(bins)
    meter.observe(minutes(0), 5.0)
    for minute in range(5, 181, 5):  # the update tick confirms the unchanged counter
        meter.observe(minutes(0), 5.0, seen=minutes(minute))
    meter.observe(minutes(190), 5.1, seen=minutes(190))
    night = bins.actual(minutes(0), minutes(160))
    assert night.energy_kwh == 0.0
    assert night.coverage == pytest.approx(1.0)
    morning = bins.actual(minutes(160), minutes(190))
    assert morning.energy_kwh == pytest.approx(0.1)
    assert morning.coverage == pytest.approx(1.0)


def test_unwatched_time_is_a_gap():
    bins = BinStore()
    meter = EnergyMeter(bins, max_gap=timedelta(minutes=20))
    meter.observe(minutes(0), 5.0)
    meter.observe(minutes(180), 5.3)
    measured = bins.actual(minutes(0), minutes(180))
    assert measured.energy_kwh == pytest.approx(0.3)
    assert measured.coverage == 0.0
    assert measured.known == pytest.approx(1.0)


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


def test_energy_meter_restores_state_saved_by_earlier_versions():
    bins = BinStore()
    meter = EnergyMeter(bins)
    meter.restore({"time": minutes(0).isoformat(), "value": 5.0})
    meter.observe(minutes(10), 5.5)
    assert bins.actual(minutes(0), minutes(10)).energy_kwh == pytest.approx(0.5)


def test_a_counter_that_has_not_stepped_yet_is_not_final():
    """A counter that steps every 15 minutes, read every 5: the minutes since the last step
    read as zero for now, but they are not final until the next step has come."""
    bins = BinStore()
    meter = EnergyMeter(bins)
    meter.observe(minutes(1), 10.0, seen=minutes(1))
    meter.observe(minutes(16), 10.5, seen=minutes(20))
    meter.observe(minutes(16), 10.5, seen=minutes(25))
    meter.observe(minutes(16), 10.5, seen=minutes(30))
    assert meter.settled_until == minutes(16)
    meter.observe(minutes(31), 11.0, seen=minutes(35))
    assert meter.settled_until == minutes(31)
    assert bins.actual(minutes(0), minutes(35)).energy_kwh == pytest.approx(1.0)
    # After a longer standstill nothing can come any more: the night is final.
    for minute in range(40, 181, 5):
        meter.observe(minutes(31), 11.0, seen=minutes(minute))
    assert meter.settled_until == minutes(150)


def test_a_missed_daily_reset_is_still_a_reset():
    """The sensor was unavailable over midnight; the first morning value is above 10 % of
    yesterday's total but clearly a new day (Home Assistant's rule: a drop below 90 %)."""
    bins = BinStore()
    meter = EnergyMeter(bins)
    meter.observe(minutes(0), 0.8)
    meter.observe(minutes(10), 0.12)
    meter.observe(minutes(20), 0.3)
    assert bins.actual(minutes(0), minutes(20)).energy_kwh == pytest.approx(0.3)


def test_a_glitch_after_a_standstill_is_rejected():
    """The plausibility limit applies to the time the energy is spread over, not to the
    hours of standstill before it."""
    bins = BinStore()
    meter = EnergyMeter(bins, max_power_kw=10.0)
    meter.observe(minutes(0), 0.0)
    for minute in range(5, 451, 5):
        meter.observe(minutes(0), 0.0, seen=minutes(minute))
    meter.observe(minutes(455), 20.5, seen=minutes(455))  # e.g. yesterday's total again
    meter.observe(minutes(460), 0.1, seen=minutes(460))
    assert meter.rejected == 1
    assert bins.actual(minutes(0), minutes(460)).energy_kwh == pytest.approx(0.1)


def test_a_step_after_a_gap_does_not_claim_the_unwatched_time():
    bins = BinStore()
    meter = EnergyMeter(bins)
    meter.observe(minutes(0), 5.0, seen=minutes(0))
    meter.observe(minutes(0), 5.0, seen=minutes(5))
    # unavailable from 5 to 35, back with the same value
    meter.observe(minutes(0), 5.0, seen=minutes(35))
    meter.observe(minutes(45), 6.0, seen=minutes(45))
    assert bins.actual(minutes(0), minutes(45)).energy_kwh == pytest.approx(1.0)
    for start, item in bins.items():
        assert item.covered_s + item.gap_s <= 300 + 1e-9, start
    assert bins.actual(minutes(15), minutes(35)).coverage == 0.0
    assert bins.actual(minutes(35), minutes(45)).coverage == pytest.approx(1.0)


def test_energy_after_a_reset_in_a_gap_belongs_to_the_latest_minutes():
    """Unavailable overnight, the first morning reading is the energy since the reset."""
    bins = BinStore()
    meter = EnergyMeter(bins)
    meter.observe(minutes(0), 9.1, seen=minutes(0))
    meter.observe(minutes(900), 0.6, seen=minutes(900))
    night = bins.actual(minutes(0), minutes(870))
    assert night.energy_kwh == 0.0
    assert bins.actual(minutes(870), minutes(900)).energy_kwh == pytest.approx(0.6)
    assert bins.actual(minutes(870), minutes(900)).coverage == 0.0


def test_power_meter_is_final_up_to_its_last_reading():
    bins = BinStore()
    meter = PowerMeter(bins)
    assert meter.settled_until is None
    meter.observe(minutes(0), 2.0)
    meter.observe(minutes(5), 2.0)
    assert meter.settled_until == minutes(5)
