"""Layer B: intraday fog factor.

On days when a whole morning runs far below a confident forecast (low stratus, fog),
the rest of the day is scaled down by the measured ratio:

    k_morning = sum actual / sum (f * forecast of record)   since the first daylight interval
    k_recent  = the same over the last hours
    k         = clamp(k_morning, k_min, 1.0)                once the gate has opened

Only downwards, and once the gate has opened the layer stays active for the rest of the
day (no decay). Selecting on low production alone also catches dull mornings that are
followed by a normal afternoon, so the gate can additionally require that the base
forecast itself was confident (narrow P10/P50 spread).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any


class FogMode(StrEnum):
    """How the fog layer is used."""

    OFF = "off"
    SHADOW = "shadow"
    ON = "on"


class FogReason(StrEnum):
    """Why the fog factor has its current value."""

    DISABLED = "disabled"
    NO_FORECAST = "no_forecast"
    WAITING = "waiting"
    GATE_CLOSED = "gate_closed"
    BASE_UNCERTAIN = "base_uncertain"
    NO_BAND = "no_band"
    ACTIVE = "active"


@dataclass(frozen=True, slots=True)
class FogGate:
    """Condition that opens the fog layer for the rest of the day.

    ``threshold`` (g): both ``k_morning`` and ``k_recent`` must be below it; ``None``
    opens the gate as soon as enough of the morning has been measured (ungated).
    ``min_confidence`` (c): the base forecast's day P10/P50 must be at least this;
    ``None`` drops the forecast-side condition.
    """

    threshold: float | None = 0.7
    min_confidence: float | None = 0.5

    @property
    def name(self) -> str:
        """Short, stable description used in attributes and reports."""
        if self.threshold is None:
            return "ungated"
        if self.min_confidence is None:
            return f"consistency {self.threshold:g}"
        return f"confident miss {self.threshold:g} / {self.min_confidence:g}"


@dataclass(frozen=True, slots=True)
class FogSettings:
    """Parameters of layer B."""

    mode: FogMode = FogMode.SHADOW
    gate: FogGate = FogGate()
    recent_hours: float = 2.0
    min_share: float = 0.05
    min_factor: float = 0.2


@dataclass(frozen=True, slots=True)
class FogInputs:
    """Energies (kWh) measured and expected so far today.

    ``expected_*`` are level-corrected forecasts of record of the measured intervals,
    ``expected_day`` is the level-corrected P50 of the whole day and ``confidence`` the
    base forecast's P10/P50 for the day (None without a band).
    """

    expected_morning: float
    actual_morning: float
    expected_recent: float
    actual_recent: float
    expected_day: float
    confidence: float | None


@dataclass(frozen=True, slots=True)
class FogState:
    """State of the fog layer for the current day."""

    k: float = 1.0
    k_morning: float | None = None
    k_recent: float | None = None
    confidence: float | None = None
    reason: FogReason = FogReason.NO_FORECAST
    active_since: datetime | None = None

    @property
    def active(self) -> bool:
        return self.active_since is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "k_morning": self.k_morning,
            "k_recent": self.k_recent,
            "confidence": self.confidence,
            "reason": self.reason.value,
            "active_since": None if self.active_since is None else self.active_since.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FogState:
        since = data.get("active_since")
        return cls(
            k=float(data.get("k", 1.0)),
            k_morning=data.get("k_morning"),
            k_recent=data.get("k_recent"),
            confidence=data.get("confidence"),
            reason=FogReason(data.get("reason", FogReason.NO_FORECAST)),
            active_since=None if since is None else datetime.fromisoformat(since),
        )


def _ratio(actual: float, expected: float) -> float | None:
    return actual / expected if expected > 0 else None


def evaluate(state: FogState, inputs: FogInputs, settings: FogSettings, now: datetime) -> FogState:
    """Next fog state. The returned ``k`` is the factor the layer would apply."""
    k_morning = _ratio(inputs.actual_morning, inputs.expected_morning)
    k_recent = _ratio(inputs.actual_recent, inputs.expected_recent)
    current = replace(state, k_morning=k_morning, k_recent=k_recent, confidence=inputs.confidence)

    if current.active:
        k = state.k if k_morning is None else min(max(k_morning, settings.min_factor), 1.0)
        return replace(current, k=k, reason=FogReason.ACTIVE)
    if inputs.expected_day <= 0:
        return replace(current, k=1.0, reason=FogReason.NO_FORECAST)
    if k_morning is None or inputs.expected_morning < settings.min_share * inputs.expected_day:
        return replace(current, k=1.0, reason=FogReason.WAITING)

    gate = settings.gate
    if gate.threshold is not None and not (
        k_morning < gate.threshold and k_recent is not None and k_recent < gate.threshold
    ):
        return replace(current, k=1.0, reason=FogReason.GATE_CLOSED)
    if gate.min_confidence is not None:
        if inputs.confidence is None:
            return replace(current, k=1.0, reason=FogReason.NO_BAND)
        if inputs.confidence < gate.min_confidence:
            return replace(current, k=1.0, reason=FogReason.BASE_UNCERTAIN)

    k = min(max(k_morning, settings.min_factor), 1.0)
    return replace(current, k=k, reason=FogReason.ACTIVE, active_since=now)
