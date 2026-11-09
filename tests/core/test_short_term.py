"""Layer B in the engine: short-term part, fog layer by nightly choice, records, restarts."""

import math
from datetime import date, datetime, time, timedelta

import pytest

from pvfc_core.engine import Corrector, Settings
from pvfc_core.fog import FogGate, FogMode, FogSettings
from pvfc_core.forecast_format import energy
from pvfc_core.intraday import FogPart, IntradaySettings, ShortTermReason
from pvfc_core.level import LevelSettings
from pvfc_core.replay import replay
from pvfc_core.score import CORRECTED, SHADOW
from pvfc_core.timeline import BIN, INTERVAL, local_time

from .synthetic import TZ, bins_from, curve, daily_snapshots, day_range, forecast

FIRST = date(2026, 11, 2)
GATE = FogGate(threshold=0.7, min_confidence=None)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return local_time(day, time(hour, minute), TZ)


def settings(mode=FogMode.ON, part=FogPart.NEVER, **intraday) -> Settings:
    return Settings(
        level_enabled=False,
        fog=FogSettings(mode=mode, gate=GATE),
        intraday=IntradaySettings(fog_part=part, **intraday),
    )


def live(engine_settings: Settings, ratio, until: datetime, days=(FIRST,)) -> Corrector:
    """A live engine fed with bins at ``ratio`` of the forecast up to ``until``."""
    engine = Corrector(engine_settings, TZ)
    start = at(days[0], 0)
    engine.set_base(forecast([*days, days[-1] + timedelta(days=1)]), start)
    pending = sorted(bins_from([p for d in days for p in curve(d)], ratio), key=lambda b: b.start)
    current = start
    while current <= until:
        while pending and pending[0].start + BIN <= current:
            item = pending.pop(0)
            engine.set_bin(item.start, item.energy_kwh, item.covered_s, item.curtailed)
        engine.update(current)
        current += BIN
    return engine


def test_the_next_intervals_follow_the_last_hour_and_fade():
    now = at(FIRST, 11)
    engine = live(settings(), 0.5, now)
    assert engine.short_term is ShortTermReason.ACTIVE
    assert engine.k_hour == pytest.approx(0.5)
    result = engine.result(now)
    base = {p.start: p.p50 for p in curve(FIRST)}
    points = {p.start: p.p50 for p in result.remaining.points}
    first = at(FIRST, 11)
    assert points[first] == pytest.approx(base[first] * math.exp(math.exp(-0.25) * math.log(0.5)))
    late = at(FIRST, 15)
    assert points[late] == pytest.approx(base[late], rel=0.01)
    assert result.fog.next_hour_factor < result.fog.rest_factor < 1.0


def test_the_rest_of_today_is_the_sum_of_its_intervals():
    now = at(FIRST, 11, 10)
    engine = live(settings(), 0.5, now)
    result = engine.result(now)
    rest = result.remaining
    current = at(FIRST, 11)
    expected = 0.0
    for point in rest.points:
        left = 1.0 if point.start >= now else (point.start + INTERVAL - now) / INTERVAL
        expected += point.p50 * 0.5 * left
    assert rest.estimate == pytest.approx(expected)
    # The current interval is the measured part plus the corrected forecast of the rest.
    today = {p.start: p.p50 for p in result.today.points}
    corrected_now = next(p for p in rest.points if p.start == current).p50
    measured_kw = 0.5 * next(p for p in curve(FIRST) if p.start == current).p50
    assert today[current] == pytest.approx(measured_kw * (1 / 3) + corrected_now * (2 / 3))


def test_little_expected_energy_waits():
    engine = live(settings(), 0.5, at(FIRST, 7, 30))
    assert engine.short_term is ShortTermReason.WAITING
    assert engine.k_hour is None


def test_nothing_measured_leaves_the_fog_factor_alone():
    engine = live(settings(), 0.0, at(FIRST, 11))
    assert engine.short_term is ShortTermReason.NOTHING_MEASURED
    result = engine.result(at(FIRST, 11))
    assert result.fog.rest_factor == pytest.approx(1.0)


def test_the_fog_layer_carries_the_rest_when_allowed():
    now = at(FIRST, 12)
    engine = live(settings(part=FogPart.ALWAYS), 0.3, now)
    assert engine.fog.active
    result = engine.result(now)
    base = {p.start: p.p50 for p in curve(FIRST)}
    late = {p.start: p.p50 for p in result.remaining.points}[at(FIRST, 15)]
    assert late == pytest.approx(base[at(FIRST, 15)] * engine.fog.k, rel=0.01)
    assert result.fog.applied == pytest.approx(engine.fog.k)
    # Not allowed: the gate opens all the same, but only the short-term part applies, which
    # has nearly faded out 3.25 h ahead.
    never = live(settings(part=FogPart.NEVER), 0.3, now)
    assert never.fog.active
    late_never = {p.start: p.p50 for p in never.result(now).remaining.points}[at(FIRST, 15)]
    fade = math.exp(math.exp(-3.25) * math.log(never.k_hour))
    assert late_never == pytest.approx(base[at(FIRST, 15)] * fade)
    assert never.result(now).fog.applied == 1.0


def test_shadow_mode_scores_layer_b_without_changing_the_forecast():
    days = day_range(FIRST, 2)
    points = [p for d in days for p in curve(d)]
    shadow = replay(daily_snapshots(days), bins_from(points, 0.5), settings(FogMode.SHADOW), TZ)
    off = replay(daily_snapshots(days), bins_from(points, 0.5), settings(FogMode.OFF), TZ)
    report, plain = shadow.reports[FIRST], off.reports[FIRST]
    assert report.variants[CORRECTED].rest == pytest.approx(plain.variants[CORRECTED].rest)
    assert report.variants[SHADOW].rest_mae < report.variants[CORRECTED].rest_mae


def test_hour_records_line_up_with_local_hours():
    engine = live(settings(), lambda s: 0.5, at(FIRST + timedelta(days=1), 0, 5))
    record = engine.tuning_days[FIRST]
    base = {}
    for point in curve(FIRST):
        if point.p50 > 0:
            hour = point.start.astimezone(TZ).hour
            base[hour] = base.get(hour, 0.0) + point.p50 * 0.5
    assert record.expected == pytest.approx(base)
    assert record.actual == pytest.approx({h: 0.5 * v for h, v in base.items()})
    assert record.asof is not None
    for hour, remaining in record.asof.items():
        assert min(remaining) >= hour
        # The base never changed, so the forecast as of each hour is the record.
        assert remaining == pytest.approx({h: v for h, v in base.items() if h >= hour})
    assert set(range(8, 17)) <= set(record.asof)


def test_the_nightly_choice_turns_to_the_fog_layer_in_a_fog_spell():
    days = day_range(FIRST, 9)
    engine = live(settings(part=FogPart.AUTO), 0.35, at(days[-1], 0, 5), days=days)
    assert len(engine.tuning.days) >= 7
    assert engine.tuning.use_fog
    assert engine.fog_used


def test_imported_days_feed_the_nightly_choice():
    engine = Corrector(settings(part=FogPart.AUTO), TZ)
    now = at(FIRST + timedelta(days=10), 6)
    rows = []
    for offset in range(10):
        hours = []
        for h in range(8, 16):
            f = 2.0 * math.sin(math.pi * (h + 0.5 - 8) / 8)
            hours.append((h, 0.35 * f, f))
        rows.append(
            (
                FIRST + timedelta(days=offset),
                sum(a for _, a, _ in hours),
                sum(f for *_, f in hours),
                tuple(hours),
            )
        )
    assert engine.import_days(rows, now) == 10
    assert engine.tuning.with_asof == 0
    assert len(engine.tuning.days) == 10
    assert engine.tuning.use_fog


def test_layer_b_state_survives_a_restart():
    now = at(FIRST + timedelta(days=1), 11)
    engine = live(settings(), 0.5, now, days=day_range(FIRST, 2))
    restored = Corrector.from_dict(engine.to_dict(), engine.settings, TZ)
    assert restored.k_hour == pytest.approx(engine.k_hour)
    assert restored.short_term is engine.short_term
    assert restored.asof.keys() == engine.asof.keys()
    for hour, remaining in engine.asof.items():
        assert restored.asof[hour] == pytest.approx(remaining, abs=1e-4)
    assert set(restored.tuning_days) == set(engine.tuning_days)
    assert restored.tuning == engine.tuning
    result, again = engine.result(now), restored.result(now)
    assert again.remaining.estimate == pytest.approx(result.remaining.estimate)


def test_without_layer_b_nothing_changes():
    now = at(FIRST, 11)
    engine = live(settings(FogMode.OFF), 0.5, now)
    assert engine.short_term is ShortTermReason.DISABLED
    result = engine.result(now)
    base_rest = energy(p for p in curve(FIRST) if p.start >= now)
    assert result.remaining.estimate == pytest.approx(base_rest)
    assert result.fog.rest_factor is None


def test_level_factor_and_short_term_part_combine():
    days = day_range(FIRST, 3)
    combined = Settings(
        level=LevelSettings(by_time=False),
        fog=FogSettings(mode=FogMode.ON, gate=GATE),
        intraday=IntradaySettings(fog_part=FogPart.NEVER),
    )
    now = at(days[-1], 11)
    engine = live(combined, 0.5, now, days=days)
    level = engine.level.factor
    assert level < 1.0
    # Measured 0.5 of the base is 0.5 / level of the level-corrected forecast.
    assert engine.k_hour == pytest.approx(0.5 / level, rel=0.02)


def test_resetting_the_level_factor_keeps_the_nightly_choice():
    days = day_range(FIRST, 9)
    now = at(days[-1], 12)
    engine = live(settings(part=FogPart.AUTO), 0.35, now, days=days)
    assert engine.fog_used
    engine.reset_learning()
    engine.recompute_level(now)
    assert engine.fog_used
    assert len(engine.tuning.days) >= 7


def test_a_state_from_before_layer_b_restores_from_history():
    days = day_range(FIRST, 9)
    now = at(days[-1], 12)
    engine = live(settings(part=FogPart.AUTO), 0.35, now, days=days)
    state = engine.to_dict()
    del state["intraday"]
    restored = Corrector.from_dict(state, engine.settings, TZ)
    assert restored.tuning_days == {}
    assert restored.tuning.with_asof == 0
    assert len(restored.tuning.days) >= 7
    assert restored.fog_used
    restored.update(now)
    assert restored.k_hour == pytest.approx(engine.k_hour)


def test_curtailed_hours_are_left_out_of_the_day_record():
    engine = Corrector(settings(), TZ)
    start = at(FIRST, 0)
    engine.set_base(forecast([FIRST, FIRST + timedelta(days=1)]), start)
    curtailed = {at(FIRST, 12), at(FIRST, 12, 30)}
    pending = sorted(bins_from(curve(FIRST), 0.5, curtailed=curtailed), key=lambda b: b.start)
    current = start
    while current <= at(FIRST + timedelta(days=1), 0, 5):
        while pending and pending[0].start + BIN <= current:
            item = pending.pop(0)
            engine.set_bin(item.start, item.energy_kwh, item.covered_s, item.curtailed)
        engine.update(current)
        current += BIN
    record = engine.tuning_days[FIRST]
    assert 12 not in record.expected
    assert 12 not in record.actual
    assert all(12 not in remaining for remaining in record.asof.values())
    assert 11 in record.expected
    assert 13 in record.expected


def test_old_days_do_not_decide():
    days = day_range(FIRST, 9)
    engine = live(settings(part=FogPart.AUTO), 0.35, at(days[-1], 12), days=days)
    later = days[-1] + timedelta(days=40)
    engine.day = later
    engine.recompute_level(at(later, 6))
    assert engine.tuning.days == ()
    assert not engine.fog_used
