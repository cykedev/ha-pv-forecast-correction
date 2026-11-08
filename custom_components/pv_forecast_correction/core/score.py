"""Checkpoints and errors: is the corrected forecast better than the base?

At fixed local times the forecasts are frozen ("as of"): the day total at 07:00, the
rest of the day at 09:00 ... 14:00 and tomorrow's total at 21:00. After the day they are
compared with what was measured. Errors are signed, forecast minus actual, in kWh.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import Any

DAY_CHECKPOINT = time(7)
REST_CHECKPOINTS = tuple(time(hour) for hour in range(9, 15))
DAY_AHEAD_CHECKPOINT = time(21)
CAPTURE_WINDOW = timedelta(minutes=15)
"""A checkpoint is captured by the first update within this window after its time."""

BASE = "base"
CORRECTED = "corrected"
SHADOW = "shadow"


def checkpoint_key(wall: time) -> str:
    """Label of a checkpoint, e.g. ``"09:00"``."""
    return wall.strftime("%H:%M")


@dataclass(frozen=True, slots=True)
class Band:
    """Energy in kWh with an optional P10/P90 band."""

    p50: float
    p10: float | None = None
    p90: float | None = None

    def scaled(self, factor: float, offset: float = 0.0) -> Band:
        """``offset + factor * band``."""
        return Band(
            offset + self.p50 * factor,
            None if self.p10 is None else offset + self.p10 * factor,
            None if self.p90 is None else offset + self.p90 * factor,
        )

    def to_list(self) -> list[float | None]:
        return [self.p50, self.p10, self.p90]

    @classmethod
    def from_list(cls, data: list[float | None]) -> Band:
        p50, p10, p90 = data
        return cls(float(p50 or 0.0), p10, p90)


def _bands_to_dict(bands: Mapping[str, Band]) -> dict[str, list[float | None]]:
    return {name: band.to_list() for name, band in bands.items()}


def _bands_from_dict(data: Mapping[str, list[float | None]]) -> dict[str, Band]:
    return {name: Band.from_list(values) for name, values in data.items()}


@dataclass(slots=True)
class DayCheckpoints:
    """Forecasts frozen at the checkpoints of one day, per variant."""

    day_total: dict[str, Band] = field(default_factory=dict)
    rest: dict[str, dict[str, Band]] = field(default_factory=dict)
    day_ahead: dict[str, Band] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day_total": _bands_to_dict(self.day_total),
            "rest": {key: _bands_to_dict(value) for key, value in self.rest.items()},
            "day_ahead": _bands_to_dict(self.day_ahead),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DayCheckpoints:
        return cls(
            day_total=_bands_from_dict(data.get("day_total", {})),
            rest={key: _bands_from_dict(value) for key, value in data.get("rest", {}).items()},
            day_ahead=_bands_from_dict(data.get("day_ahead", {})),
        )


@dataclass(frozen=True, slots=True)
class VariantErrors:
    """Errors of one forecast variant for one day (forecast minus actual, kWh)."""

    day: float | None = None
    day_ahead: float | None = None
    rest: dict[str, float] = field(default_factory=dict)
    day_p10_held: bool | None = None
    day_p90_held: bool | None = None

    @property
    def rest_mae(self) -> float | None:
        """Mean absolute rest-of-day error over the captured checkpoints."""
        if not self.rest:
            return None
        return sum(abs(value) for value in self.rest.values()) / len(self.rest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "day_ahead": self.day_ahead,
            "rest": dict(self.rest),
            "day_p10_held": self.day_p10_held,
            "day_p90_held": self.day_p90_held,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VariantErrors:
        return cls(
            day=data.get("day"),
            day_ahead=data.get("day_ahead"),
            rest={key: float(value) for key, value in data.get("rest", {}).items()},
            day_p10_held=data.get("day_p10_held"),
            day_p90_held=data.get("day_p90_held"),
        )


@dataclass(frozen=True, slots=True)
class DayReport:
    """How the variants did on one day."""

    day: date
    actual_kwh: float
    actual_rest: dict[str, float]
    complete: bool
    level_factor: float
    fog_active: bool
    variants: dict[str, VariantErrors]
    base_confidence: float | None = None
    """P10/P50 of the base day total as of 07:00 (None without a band)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "actual_kwh": self.actual_kwh,
            "actual_rest": dict(self.actual_rest),
            "complete": self.complete,
            "level_factor": self.level_factor,
            "fog_active": self.fog_active,
            "base_confidence": self.base_confidence,
            "variants": {name: errors.to_dict() for name, errors in self.variants.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DayReport:
        return cls(
            day=date.fromisoformat(data["day"]),
            actual_kwh=float(data["actual_kwh"]),
            actual_rest={key: float(v) for key, v in data.get("actual_rest", {}).items()},
            complete=bool(data["complete"]),
            level_factor=float(data.get("level_factor", 1.0)),
            fog_active=bool(data.get("fog_active", False)),
            variants={
                name: VariantErrors.from_dict(errors)
                for name, errors in data.get("variants", {}).items()
            },
            base_confidence=data.get("base_confidence"),
        )


def build_report(
    day: date,
    checkpoints: DayCheckpoints,
    actual_kwh: float,
    actual_rest: Mapping[str, float],
    *,
    complete: bool,
    level_factor: float,
    fog_active: bool,
) -> DayReport:
    """Compare the frozen forecasts of a finished day with the measurements."""
    names = set(checkpoints.day_total) | set(checkpoints.day_ahead)
    for bands in checkpoints.rest.values():
        names |= set(bands)
    variants: dict[str, VariantErrors] = {}
    for name in sorted(names):
        day_band = checkpoints.day_total.get(name)
        ahead_band = checkpoints.day_ahead.get(name)
        rest = {
            key: bands[name].p50 - actual_rest[key]
            for key, bands in checkpoints.rest.items()
            if name in bands and key in actual_rest
        }
        variants[name] = VariantErrors(
            day=None if day_band is None else day_band.p50 - actual_kwh,
            day_ahead=None if ahead_band is None else ahead_band.p50 - actual_kwh,
            rest=rest,
            day_p10_held=None
            if day_band is None or day_band.p10 is None
            else actual_kwh >= day_band.p10,
            day_p90_held=None
            if day_band is None or day_band.p90 is None
            else actual_kwh <= day_band.p90,
        )
    base_day = checkpoints.day_total.get(BASE)
    confidence = None
    if base_day is not None and base_day.p10 is not None and base_day.p50 > 0:
        confidence = base_day.p10 / base_day.p50
    return DayReport(
        day=day,
        actual_kwh=actual_kwh,
        actual_rest=dict(actual_rest),
        complete=complete,
        level_factor=level_factor,
        fog_active=fog_active,
        variants=variants,
        base_confidence=confidence,
    )


@dataclass(frozen=True, slots=True)
class Summary:
    """Mean absolute errors of one variant over many days."""

    days: int
    rest_mae: float | None
    rest_mae_by_checkpoint: dict[str, float]
    day_mae: float | None
    day_ahead_mae: float | None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(reports: Iterable[DayReport], variant: str) -> Summary:
    """Aggregate complete days. Rest-of-day MAE weighs every (day, checkpoint) equally."""
    rest_all: list[float] = []
    by_checkpoint: dict[str, list[float]] = {}
    day_errors: list[float] = []
    ahead_errors: list[float] = []
    days = 0
    for report in reports:
        errors = report.variants.get(variant)
        if not report.complete or errors is None:
            continue
        days += 1
        for key, value in errors.rest.items():
            rest_all.append(abs(value))
            by_checkpoint.setdefault(key, []).append(abs(value))
        if errors.day is not None:
            day_errors.append(abs(errors.day))
        if errors.day_ahead is not None:
            ahead_errors.append(abs(errors.day_ahead))
    return Summary(
        days=days,
        rest_mae=_mean(rest_all),
        rest_mae_by_checkpoint={
            key: sum(values) / len(values) for key, values in sorted(by_checkpoint.items())
        },
        day_mae=_mean(day_errors),
        day_ahead_mae=_mean(ahead_errors),
    )


# ---------------------------------------------------------------------- evaluation

CLEAR = "clear"
MIXED = "mixed"
DULL = "dull"


def classify_days(
    totals: Mapping[date, float],
    *,
    window_days: int = 15,
    clear_from: float = 0.8,
    dull_below: float = 0.4,
) -> dict[date, str]:
    """Day class from measured production alone (no forecast involved).

    A day's clearness is its production over the highest production within
    ``window_days`` before or after it, a stand-in for clear-sky production that
    follows the season.
    """
    classes: dict[date, str] = {}
    for day, total in totals.items():
        span = timedelta(days=window_days)
        peak = max(v for d, v in totals.items() if abs(d - day) <= span)
        clearness = total / peak if peak > 0 else 0.0
        if clearness >= clear_from:
            classes[day] = CLEAR
        elif clearness < dull_below:
            classes[day] = DULL
        else:
            classes[day] = MIXED
    return classes


def spells(days: Iterable[date]) -> list[list[date]]:
    """Group days into runs of consecutive calendar days."""
    runs: list[list[date]] = []
    for day in sorted(set(days)):
        if runs and day - runs[-1][-1] == timedelta(days=1):
            runs[-1].append(day)
        else:
            runs.append([day])
    return runs


@dataclass(frozen=True, slots=True)
class Criterion:
    """One condition of a gate, with the numbers behind it."""

    name: str
    candidate: float | None
    reference: float | None
    limit: float
    passed: bool | None

    @property
    def change(self) -> float | None:
        """Relative change of the candidate against the reference."""
        if self.candidate is None or not self.reference:
            return None
        return self.candidate / self.reference - 1


_Paired = tuple[
    list[float], list[float], dict[str, tuple[list[float], list[float]]], list[float], list[float]
]


def _paired_errors(
    candidate: Mapping[date, DayReport],
    reference: Mapping[date, DayReport],
    cand_variant: str,
    ref_variant: str,
    days: Iterable[date],
) -> _Paired:
    rest_c: list[float] = []
    rest_r: list[float] = []
    by_cp: dict[str, tuple[list[float], list[float]]] = {}
    ahead_c: list[float] = []
    ahead_r: list[float] = []
    for day in days:
        c_report, r_report = candidate.get(day), reference.get(day)
        if c_report is None or r_report is None or not (c_report.complete and r_report.complete):
            continue
        c_err, r_err = c_report.variants.get(cand_variant), r_report.variants.get(ref_variant)
        if c_err is None or r_err is None:
            continue
        for key in sorted(set(c_err.rest) & set(r_err.rest)):
            rest_c.append(abs(c_err.rest[key]))
            rest_r.append(abs(r_err.rest[key]))
            pair = by_cp.setdefault(key, ([], []))
            pair[0].append(abs(c_err.rest[key]))
            pair[1].append(abs(r_err.rest[key]))
        if c_err.day_ahead is not None and r_err.day_ahead is not None:
            ahead_c.append(abs(c_err.day_ahead))
            ahead_r.append(abs(r_err.day_ahead))
    return rest_c, rest_r, by_cp, ahead_c, ahead_r


def _criterion(name: str, cand: list[float], ref: list[float], limit: float) -> Criterion:
    c, r = _mean(cand), _mean(ref)
    if c is None or r is None:
        return Criterion(name, c, r, limit, None)
    passed = c <= r * (1 + limit) if r > 0 else c <= r
    return Criterion(name, c, r, limit, passed)


def gate_criteria(
    candidate: Mapping[date, DayReport],
    reference: Mapping[date, DayReport],
    *,
    cand_variant: str = CORRECTED,
    ref_variant: str = BASE,
    classes: Mapping[date, str] | None = None,
    min_gain: float = 0.15,
    checkpoint_tolerance: float = 0.03,
    class_tolerance: float = 0.05,
    classes_checked: tuple[str, ...] = (CLEAR,),
) -> list[Criterion]:
    """The pre-registered conditions for switching a layer on by default.

    - rest-of-day MAE over all checkpoints at least ``min_gain`` below the reference,
    - no checkpoint worse than the reference by more than ``checkpoint_tolerance``,
    - each class in ``classes_checked`` not worse by more than ``class_tolerance``,
    - day-ahead (as of the evening before) not worse.
    Errors are paired: only days and checkpoints present in both runs count.
    """
    days = sorted(set(candidate) & set(reference))
    rest_c, rest_r, by_cp, ahead_c, ahead_r = _paired_errors(
        candidate, reference, cand_variant, ref_variant, days
    )
    result = [_criterion("rest of day", rest_c, rest_r, -min_gain)]
    for key, (c, r) in sorted(by_cp.items()):
        result.append(_criterion(f"checkpoint {key}", c, r, checkpoint_tolerance))
    for day_class in classes_checked:
        selected = [d for d in days if classes is not None and classes.get(d) == day_class]
        c, r, _, _, _ = _paired_errors(candidate, reference, cand_variant, ref_variant, selected)
        result.append(_criterion(f"{day_class} days", c, r, class_tolerance))
    result.append(_criterion("day-ahead", ahead_c, ahead_r, 0.0))
    return result
