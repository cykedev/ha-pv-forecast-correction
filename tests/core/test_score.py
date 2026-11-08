"""Day classes, spells and the pre-registered gate criteria."""

from datetime import date, timedelta

import pytest

from pvfc_core.score import (
    BASE,
    CLEAR,
    CORRECTED,
    DULL,
    MIXED,
    Band,
    DayCheckpoints,
    build_report,
    classify_days,
    gate_criteria,
    spells,
    summarize,
)

DAY = date(2026, 11, 8)


def report(day, base_err, corrected_err, actual=20.0, ahead=(2.0, 1.0)):
    checkpoints = DayCheckpoints(
        day_total={BASE: Band(actual + base_err), CORRECTED: Band(actual + corrected_err)},
        rest={
            "09:00": {BASE: Band(10 + base_err), CORRECTED: Band(10 + corrected_err)},
            "12:00": {BASE: Band(5 + base_err), CORRECTED: Band(5 + corrected_err)},
        },
        day_ahead={BASE: Band(actual + ahead[0]), CORRECTED: Band(actual + ahead[1])},
    )
    return build_report(
        day,
        checkpoints,
        actual,
        {"09:00": 10.0, "12:00": 5.0},
        complete=True,
        level_factor=1.0,
        fog_active=False,
    )


def test_build_report_signs_and_band_hits():
    checkpoints = DayCheckpoints(day_total={BASE: Band(25.0, 18.0, 30.0)})
    result = build_report(
        DAY, checkpoints, 20.0, {}, complete=True, level_factor=1.0, fog_active=False
    )
    errors = result.variants[BASE]
    assert errors.day == pytest.approx(5.0)
    assert errors.day_p10_held is True
    assert errors.day_p90_held is True
    assert errors.rest_mae is None


def test_summary_and_criteria():
    reports = {DAY + timedelta(days=i): report(DAY + timedelta(days=i), 4.0, 1.0) for i in range(5)}
    summary = summarize(reports.values(), CORRECTED)
    assert summary.days == 5
    assert summary.rest_mae == pytest.approx(1.0)
    assert summary.day_ahead_mae == pytest.approx(1.0)
    classes = dict.fromkeys(reports, CLEAR)
    criteria = gate_criteria(reports, reports, classes=classes)
    assert all(c.passed for c in criteria)
    assert criteria[0].change == pytest.approx(-0.75)


def test_criteria_fail_when_a_checkpoint_gets_worse():
    candidate = {DAY: report(DAY, 0.0, 0.0)}
    candidate[DAY].variants[CORRECTED].rest["12:00"] = 3.0
    reference = {DAY: report(DAY, 2.0, 2.0)}
    criteria = {c.name: c for c in gate_criteria(candidate, reference, ref_variant=CORRECTED)}
    assert criteria["checkpoint 12:00"].passed is False
    assert criteria["checkpoint 09:00"].passed is True


def test_classes_follow_the_seasonal_peak():
    totals = {DAY + timedelta(days=i): v for i, v in enumerate([30, 20, 5, 29, 31])}
    classes = classify_days(totals)
    assert classes[DAY] == CLEAR
    assert classes[DAY + timedelta(days=1)] == MIXED
    assert classes[DAY + timedelta(days=2)] == DULL


def test_spells_group_consecutive_days():
    days = [DAY, DAY + timedelta(days=1), DAY + timedelta(days=3)]
    assert spells(days) == [[DAY, DAY + timedelta(days=1)], [DAY + timedelta(days=3)]]
