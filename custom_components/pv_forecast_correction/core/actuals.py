"""Measured production: energy per 5-minute bin, data coverage and curtailment.

Readings come either from a cumulative energy sensor (kWh, may reset, e.g. daily) or
from a power sensor (kW, the sum of several sensors is fed as one value). Both are
turned into energy per bin. A bin also remembers how many of its seconds were covered
by regular readings and how many only received energy spread from a longer gap, so gaps
(restarts, unavailable sensors) are visible instead of being counted as zero production
or as measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .timeline import BIN, BIN_SECONDS, INTERVAL, as_utc, floor_to, overlap_seconds, steps

MIN_COVERAGE = 0.9
"""Share of an interval that must be covered by readings to count as measured."""


@dataclass(slots=True)
class Bin:
    """Energy in one 5-minute bin."""

    energy_kwh: float = 0.0
    covered_s: float = 0.0
    curtailed: bool = False
    gap_s: float = 0.0
    """Seconds that received energy spread from a gap longer than a regular reading."""


@dataclass(frozen=True, slots=True)
class IntervalActual:
    """Measured energy over a time range.

    ``coverage`` is the share covered by regular readings (good enough to learn from),
    ``known`` the share whose energy is known at all (regular readings or energy spread
    from a gap). The rest is unknown.
    """

    energy_kwh: float
    coverage: float
    curtailed: bool
    known: float = 1.0

    @property
    def measured(self) -> bool:
        """True when enough of the range was covered by readings."""
        return self.coverage >= MIN_COVERAGE


class BinStore:
    """Energy, coverage and curtailment per 5-minute bin."""

    def __init__(self) -> None:
        self._bins: dict[datetime, Bin] = {}

    def __len__(self) -> int:
        return len(self._bins)

    def items(self) -> list[tuple[datetime, Bin]]:
        """All bins in time order."""
        return sorted(self._bins.items())

    def _bin(self, start: datetime) -> Bin:
        found = self._bins.get(start)
        if found is None:
            found = self._bins[start] = Bin()
        return found

    def add_energy(self, start: datetime, end: datetime, kwh: float, *, covered: bool) -> None:
        """Spread ``kwh`` evenly over ``[start, end)``; optionally mark it as covered."""
        start, end = as_utc(start), as_utc(end)
        if end <= start:
            return
        total = (end - start).total_seconds()
        for bin_start in steps(floor_to(start, BIN), end, BIN):
            seconds = overlap_seconds(start, end, bin_start, bin_start + BIN)
            if seconds <= 0:
                continue
            target = self._bin(bin_start)
            target.energy_kwh += kwh * seconds / total
            if covered:
                target.covered_s = min(BIN_SECONDS, target.covered_s + seconds)
            else:
                target.gap_s = min(BIN_SECONDS - target.covered_s, target.gap_s + seconds)

    def mark_curtailed(self, start: datetime, end: datetime) -> None:
        """Flag every bin that overlaps ``[start, end)`` (or contains ``start``) as curtailed."""
        start, end = as_utc(start), as_utc(end)
        for bin_start in steps(floor_to(start, BIN), max(end, start + timedelta(seconds=1)), BIN):
            self._bin(bin_start).curtailed = True

    def set_bin(
        self, start: datetime, kwh: float, covered_s: float, curtailed: bool, gap_s: float = 0.0
    ) -> None:
        """Replace one bin (used when replaying archived bins)."""
        covered = min(covered_s, BIN_SECONDS)
        self._bins[floor_to(start, BIN)] = Bin(
            kwh, covered, curtailed, min(gap_s, BIN_SECONDS - covered)
        )

    def actual(self, start: datetime, end: datetime) -> IntervalActual:
        """Energy, coverage and curtailment over ``[start, end)``."""
        start, end = as_utc(start), as_utc(end)
        span = (end - start).total_seconds()
        if span <= 0:
            return IntervalActual(0.0, 1.0, False, 1.0)
        energy = covered = gap = 0.0
        curtailed = False
        for bin_start in steps(floor_to(start, BIN), end, BIN):
            found = self._bins.get(bin_start)
            if found is None:
                continue
            seconds = overlap_seconds(start, end, bin_start, bin_start + BIN)
            share = seconds / BIN_SECONDS
            energy += found.energy_kwh * share
            covered += found.covered_s * share
            gap += found.gap_s * share
            curtailed = curtailed or (found.curtailed and seconds > 0)
        return IntervalActual(
            energy, min(1.0, covered / span), curtailed, min(1.0, (covered + gap) / span)
        )

    def interval(self, start: datetime) -> IntervalActual:
        """Energy over the 30-minute interval starting at ``start``."""
        return self.actual(start, start + INTERVAL)

    def prune(self, before: datetime) -> None:
        """Drop bins that end before ``before``."""
        cutoff = as_utc(before)
        for start in [s for s in self._bins if s + BIN <= cutoff]:
            del self._bins[start]

    def to_dict(self) -> dict[str, list[list[float]]]:
        """Compact serialisation: ``[unix_start, kwh, covered_s, curtailed, gap_s]`` rows."""
        return {
            "bins": [
                [int(start.timestamp()), b.energy_kwh, b.covered_s, int(b.curtailed), b.gap_s]
                for start, b in self.items()
            ]
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BinStore:
        store = cls()
        for row in data.get("bins", []):
            start = datetime.fromtimestamp(int(row[0]), UTC)
            gap = float(row[4]) if len(row) > 4 else 0.0
            store.set_bin(start, float(row[1]), float(row[2]), bool(row[3]), gap)
        return store


class EnergyMeter:
    """Turns readings of a cumulative energy sensor (kWh) into bin energy.

    Feed each reading with the time it was reported. The energy between two readings is
    spread evenly over the time between them, so nothing is lost across gaps; gaps longer
    than ``max_gap`` (e.g. Home Assistant was down) are marked as gaps, not as covered.
    Meters with a coarse resolution report rarely in weak light, hence the generous
    default.

    A drop to less than ``reset_share`` of the previous reading is a counter reset (a
    daily counter at midnight); the new reading is then the energy since the reset. A
    smaller drop is a correction or jitter: it adds nothing and the higher reading stays
    the baseline. A rise that would mean more than ``max_power_kw`` on average (a glitch
    such as a lifetime meter briefly reading 0) is rejected and counted in ``rejected``.
    """

    def __init__(
        self,
        bins: BinStore,
        *,
        max_gap: timedelta = timedelta(hours=2),
        reset_share: float = 0.1,
        max_power_kw: float | None = None,
    ) -> None:
        self._bins = bins
        self._max_gap = max_gap
        self._reset_share = reset_share
        self.max_power_kw = max_power_kw
        self.rejected = 0
        self._last: tuple[datetime, float] | None = None

    def observe(self, when: datetime, value: float | None) -> None:
        """Feed a reading; ``None`` (unavailable) is skipped, the next reading bridges it."""
        if value is None:
            return
        when = as_utc(when)
        if self._last is None:
            self._last = (when, value)
            return
        last_time, last_value = self._last
        if when <= last_time:
            return
        delta = value - last_value
        baseline = value
        if delta < 0:
            if value <= self._reset_share * last_value:
                delta = max(value, 0.0)
            else:
                delta, baseline = 0.0, last_value
        hours = (when - last_time).total_seconds() / 3600
        if self.max_power_kw is not None and delta > self.max_power_kw * hours:
            self.rejected += 1
            self._last = (when, value)
            return
        self._bins.add_energy(last_time, when, delta, covered=when - last_time <= self._max_gap)
        self._last = (when, baseline)

    def to_dict(self) -> dict[str, Any]:
        if self._last is None:
            return {}
        return {"time": self._last[0].isoformat(), "value": self._last[1]}

    def restore(self, data: dict[str, Any]) -> None:
        if data.get("time") is not None:
            self._last = (as_utc(datetime.fromisoformat(data["time"])), float(data["value"]))


class PowerMeter:
    """Integrates readings of a power sensor (kW) into bin energy.

    The last reading holds until the next one (like a state in Home Assistant). A gap
    longer than ``max_hold`` is left uncovered rather than guessed.
    """

    def __init__(self, bins: BinStore, *, max_hold: timedelta = timedelta(minutes=20)) -> None:
        self._bins = bins
        self._max_hold = max_hold
        self._last: tuple[datetime, float | None] | None = None

    def observe(self, when: datetime, value: float | None) -> None:
        """Feed a reading in kW; ``None`` marks the sensor as unavailable from now on.

        A reading that arrives with an earlier time than the previous one is taken as
        simultaneous with it, so a change of state is never lost.
        """
        when = as_utc(when)
        if self._last is not None:
            last_time, last_value = self._last
            if when <= last_time:
                self._last = (last_time, value)
                return
            if last_value is not None and when - last_time <= self._max_hold:
                hours = (when - last_time).total_seconds() / 3600
                self._bins.add_energy(last_time, when, max(last_value, 0.0) * hours, covered=True)
        self._last = (when, value)

    def to_dict(self) -> dict[str, Any]:
        if self._last is None:
            return {}
        return {"time": self._last[0].isoformat(), "value": self._last[1]}

    def restore(self, data: dict[str, Any]) -> None:
        if data.get("time") is not None:
            value = data.get("value")
            self._last = (
                as_utc(datetime.fromisoformat(data["time"])),
                None if value is None else float(value),
            )


class CurtailmentTracker:
    """Marks bins as curtailed while a curtailment signal is on."""

    def __init__(self, bins: BinStore, *, max_hold: timedelta = timedelta(minutes=20)) -> None:
        self._bins = bins
        self._max_hold = max_hold
        self._last: tuple[datetime, bool] | None = None

    def observe(self, when: datetime, active: bool | None) -> None:
        """Feed the signal; ``None`` (unknown) ends a running curtailment period."""
        when = as_utc(when)
        if self._last is not None:
            last_time, last_active = self._last
            when = max(when, last_time)
            if last_active:
                end = min(when, last_time + self._max_hold)
                self._bins.mark_curtailed(last_time, end)
        if active:
            self._bins.mark_curtailed(when, when)
        self._last = (when, bool(active))

    def to_dict(self) -> dict[str, Any]:
        if self._last is None:
            return {}
        return {"time": self._last[0].isoformat(), "active": self._last[1]}

    def restore(self, data: dict[str, Any]) -> None:
        if data.get("time") is not None:
            self._last = (as_utc(datetime.fromisoformat(data["time"])), bool(data["active"]))
