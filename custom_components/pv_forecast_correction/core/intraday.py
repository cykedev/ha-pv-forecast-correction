"""Layer B, short-term part: follow the last hour, and decide each night whether the fog
layer may carry the rest of the day.

The fog layer (``fog.py``) scales the rest of the day by the morning's ratio once a dull
morning has been confirmed. On its own it reacts late (it waits for a share of the day)
and it only knows one level for all remaining hours. The short-term part adds what the
last hour of final measurements says:

    k_hour = measured / expected over the last hour (expected = corrected forecast of
             record: level factor and, when applied, weather factor)

and, for an interval ``tau`` hours ahead (from the end of the measurements to the middle
of the interval), fades from it into the fog factor:

    m(tau) = exp(e^(-tau / T) * ln k_hour + (1 - e^(-tau / T)) * ln k_fog)

``k_fog`` is the fog factor while the fog layer is active and may be used, else 1, so
without fog the last hour simply fades out over about ``T`` = 1 h.

Whether the fog layer may be used is decided each night from the last days ("auto"): both
variants are scored on hourly records of those days, and the fog layer is used only when
it did clearly better. In fog season it does (fog persists); in summer it does not (dull
mornings clear up), so no seasonal switch is needed. ``always`` and ``never`` override.

The records hold per local clock hour the expected energy of record, the measured energy
and, for each full hour, the corrected forecast of the remaining hours as it stood then
(``asof``). Days without ``asof`` (imported from the recorder) use their record instead,
which knows later base updates and so rates the base a little too well.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from .fog import FogSettings


class FogPart(StrEnum):
    """Whether the fog layer may carry the rest of the day."""

    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


class ShortTermReason(StrEnum):
    """Why the short-term ratio has its current value."""

    DISABLED = "disabled"
    WAITING = "waiting"
    """Too little energy expected in the last hour to form a ratio."""
    NOTHING_MEASURED = "nothing_measured"
    """Nothing measured in the last hour although production was expected (snow on the
    panels, an outage or a stuck meter): the fog factor alone applies."""
    ACTIVE = "active"


@dataclass(frozen=True, slots=True)
class IntradaySettings:
    """Parameters of the short-term part."""

    enabled: bool = True
    fog_part: FogPart = FogPart.AUTO
    decay_h: float = 1.0
    """T: hours over which the last hour's ratio fades."""
    min_ratio: float = 0.2
    max_ratio: float = 2.0
    min_share: float = 0.03
    """The last hour needs at least this share of the day's expected energy."""
    days: int = 14
    """Days the nightly choice looks back on."""
    min_days: int = 7
    """Fewer days than this: the fog layer is not used (auto)."""
    margin: float = 0.05
    """The fog layer must score at least this much better to be used (auto)."""

    def ratio(self, actual: float, expected: float, day_expected: float) -> float | None:
        """Clamped measured/expected over the last hour, or None while too little is
        expected or nothing at all was measured (then the fog factor alone applies)."""
        if expected <= 0 or expected < self.min_share * day_expected or actual <= 0:
            return None
        return min(max(actual / expected, self.min_ratio), self.max_ratio)


def multiplier(k_hour: float | None, k_fog: float, lead_h: float, decay_h: float) -> float:
    """Factor for an interval ``lead_h`` hours ahead."""
    if k_hour is None:
        return k_fog
    fade = math.exp(-max(lead_h, 0.0) / decay_h)
    return math.exp(fade * math.log(k_hour) + (1.0 - fade) * math.log(k_fog))


@dataclass(frozen=True, slots=True)
class TuningDay:
    """Hourly sums of one day (kWh per local clock hour)."""

    day: date
    expected: Mapping[int, float]
    actual: Mapping[int, float]
    asof: Mapping[int, Mapping[int, float]] | None = None
    """Per full hour h, the corrected forecast of every hour >= h as it stood at h."""
    confidence: float | None = None
    """P10/P50 of the base day total as of 07:00, for the fog gate's confidence condition."""

    def forecast_at(self, hour: int) -> Mapping[int, float]:
        if self.asof is not None and hour in self.asof:
            return self.asof[hour]
        return {h: e for h, e in self.expected.items() if h >= hour}

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "day": self.day.isoformat(),
            "expected": [[h, round(v, 4)] for h, v in sorted(self.expected.items())],
            "actual": [[h, round(v, 4)] for h, v in sorted(self.actual.items())],
        }
        if self.asof is not None:
            data["asof"] = [
                [h, [[j, round(v, 4)] for j, v in sorted(f.items())]]
                for h, f in sorted(self.asof.items())
            ]
        if self.confidence is not None:
            data["confidence"] = round(self.confidence, 4)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TuningDay:
        asof = data.get("asof")
        return cls(
            day=date.fromisoformat(data["day"]),
            expected={int(h): float(v) for h, v in data.get("expected", [])},
            actual={int(h): float(v) for h, v in data.get("actual", [])},
            asof=None
            if asof is None
            else {int(h): {int(j): float(v) for j, v in f} for h, f in asof},
            confidence=data.get("confidence"),
        )


def scoring_hours(day: TuningDay) -> list[int]:
    """Full hours of daylight: the hour before produced (expected) and some is still ahead."""
    return [
        h
        for h in range(1, 24)
        if day.expected.get(h - 1, 0.0) > 0
        and any(day.expected.get(j, 0.0) > 0 for j in range(h, 24))
    ]


def day_loss(day: TuningDay, use_fog: bool, fog: FogSettings, settings: IntradaySettings) -> float:
    """Absolute error (kWh) on one day: the next three hours at every full daylight hour
    plus the rest of the day at every one but the first. The fog layer is followed hour
    by hour with its own gate; its confidence condition uses the day's P10/P50 as of 07:00
    (days without it skip the condition). Without the short-term part, only the fog layer
    is scored against the plain forecast."""
    day_expected = sum(day.expected.values())
    hours = scoring_hours(day)
    total = 0.0
    active, k_fog = False, 1.0
    recent_hours = math.ceil(fog.recent_hours)  # whole hours: 0.5 h is the last hour
    confident = (
        fog.gate.min_confidence is None
        or day.confidence is None
        or day.confidence >= fog.gate.min_confidence
    )
    for index, hour in enumerate(hours):
        before = [h for h in day.expected if h < hour]
        exp_m = sum(day.expected[h] for h in before)
        act_m = sum(day.actual.get(h, 0.0) for h in before)
        recent = [h for h in before if h >= hour - recent_hours]
        exp_r = sum(day.expected[h] for h in recent)
        act_r = sum(day.actual.get(h, 0.0) for h in recent)
        k_morning = act_m / exp_m if exp_m > 0 else None
        k_recent = act_r / exp_r if exp_r > 0 else None
        if use_fog and k_morning is not None:
            if active:
                k_fog = min(max(k_morning, fog.min_factor), 1.0)
            elif (
                confident
                and exp_m >= fog.min_share * day_expected
                and (
                    fog.gate.threshold is None
                    or (
                        k_morning < fog.gate.threshold
                        and k_recent is not None
                        and k_recent < fog.gate.threshold
                    )
                )
            ):
                active, k_fog = True, min(max(k_morning, fog.min_factor), 1.0)
        k_hour = (
            settings.ratio(
                day.actual.get(hour - 1, 0.0), day.expected.get(hour - 1, 0.0), day_expected
            )
            if settings.enabled
            else None
        )
        forecast = day.forecast_at(hour)
        predicted = {
            h: kwh * multiplier(k_hour, k_fog if active else 1.0, h - hour + 0.5, settings.decay_h)
            for h, kwh in forecast.items()
            if h >= hour
        }
        if index > 0:
            rest = sum(kwh for h, kwh in day.actual.items() if h >= hour)
            total += abs(sum(predicted.values()) - rest)
        soon = range(hour, hour + 3)
        total += abs(
            sum(predicted.get(h, 0.0) for h in soon) - sum(day.actual.get(h, 0.0) for h in soon)
        )
    return total


@dataclass(frozen=True, slots=True)
class Tuning:
    """Outcome of the nightly choice."""

    use_fog: bool = False
    days: tuple[date, ...] = ()
    loss_follow: float | None = None
    """Error of the short-term part alone over ``days`` (kWh)."""
    loss_fog: float | None = None
    """Error with the fog layer carrying the rest of the day (kWh)."""
    with_asof: int = 0
    """How many of ``days`` had the forecast as of each hour (the rest used the record)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "use_fog": self.use_fog,
            "days": [d.isoformat() for d in self.days],
            "loss_follow": None if self.loss_follow is None else round(self.loss_follow, 3),
            "loss_fog": None if self.loss_fog is None else round(self.loss_fog, 3),
            "with_asof": self.with_asof,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Tuning:
        return cls(
            use_fog=bool(data.get("use_fog", False)),
            days=tuple(date.fromisoformat(d) for d in data.get("days", [])),
            loss_follow=data.get("loss_follow"),
            loss_fog=data.get("loss_fog"),
            with_asof=int(data.get("with_asof", 0)),
        )


def tune(days: Iterable[TuningDay], fog: FogSettings, settings: IntradaySettings) -> Tuning:
    """Use the fog layer when, over the most recent ``settings.days`` days, it scored at
    least ``settings.margin`` better than the short-term part alone."""
    recent = sorted(days, key=lambda d: d.day)[-settings.days :] if settings.days > 0 else []
    if len(recent) < settings.min_days:
        return Tuning(
            days=tuple(d.day for d in recent),
            with_asof=sum(1 for d in recent if d.asof is not None),
        )
    follow = sum(day_loss(d, False, fog, settings) for d in recent)
    with_fog = sum(day_loss(d, True, fog, settings) for d in recent)
    return Tuning(
        use_fog=with_fog < (1.0 - settings.margin) * follow,
        days=tuple(d.day for d in recent),
        loss_follow=follow,
        loss_fog=with_fog,
        with_asof=sum(1 for d in recent if d.asof is not None),
    )


def fog_allowed(settings: IntradaySettings, tuning: Tuning) -> bool:
    """Whether the fog layer may carry the rest of today."""
    if settings.fog_part is FogPart.ALWAYS:
        return True
    if settings.fog_part is FogPart.NEVER:
        return False
    return tuning.use_fog
