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

    Each reading carries two times: ``when`` the value took effect (its last change) and
    ``seen`` when it was last confirmed (an update tick). Confirming an unchanged value
    covers the time since the previous confirmation with zero energy: the counter stood
    still, so nothing was produced (night, end of the day). A change spreads its energy over
    the time since the previous change, at most the last ``max_step``: counters step every
    few minutes, and after a standstill the energy belongs to the latest minutes. Time that
    was not watched for longer than ``max_gap`` (Home Assistant was down, the sensor was
    unavailable) is marked as a gap: its energy is known in total but not where it fell.

    Until a standstill has lasted longer than ``max_step``, the next step may still spread
    energy over it, so bins after ``settled_until`` are provisional: they read as zero but
    the counter may not have caught up yet.

    A drop to less than ``reset_share`` of the previous reading is a counter reset (a
    daily counter at midnight, the same rule Home Assistant uses for ``total_increasing``);
    the new reading is then the energy since the reset. A smaller drop is a correction or
    jitter: it adds nothing and the higher reading stays the baseline. A rise that would
    mean more than ``max_power_kw`` on average over the time it is spread across (a glitch
    such as a lifetime meter briefly reading 0) is rejected and counted in ``rejected``.
    """

    def __init__(
        self,
        bins: BinStore,
        *,
        max_gap: timedelta = timedelta(minutes=20),
        max_step: timedelta = timedelta(minutes=30),
        reset_share: float = 0.9,
        max_power_kw: float | None = None,
    ) -> None:
        self._bins = bins
        self._max_gap = max_gap
        self._max_step = max_step
        self._reset_share = reset_share
        self.max_power_kw = max_power_kw
        self.rejected = 0
        self._value: float | None = None
        self._changed: datetime | None = None
        self._seen: datetime | None = None
        self._resumed: datetime | None = None
        """Watching resumed here after a gap; covered spreads do not reach back past it."""

    @property
    def settled_until(self) -> datetime | None:
        """Bins before this instant are final; later ones may still change."""
        if self._changed is None or self._seen is None:
            return None
        return max(self._changed, self._seen - self._max_step)

    def _confirm(self, start: datetime, end: datetime) -> None:
        """The counter did not move between ``start`` and ``end``."""
        if end > start:
            self._bins.add_energy(start, end, 0.0, covered=end - start <= self._max_gap)

    def observe(self, when: datetime, value: float | None, seen: datetime | None = None) -> None:
        """Feed a reading; ``None`` (unavailable) is skipped, the next reading bridges it."""
        if value is None:
            return
        when = as_utc(when)
        seen = max(when, as_utc(seen)) if seen is not None else when
        if self._value is None or self._changed is None or self._seen is None:
            self._value, self._changed, self._seen = value, when, seen
            return
        if seen <= self._seen and value == self._value:
            return
        if value == self._value:
            self._watch(seen)
            self._confirm(self._seen, seen)
            self._seen = seen
            return

        when = min(max(when, self._changed), seen)
        delta = value - self._value
        reset = False
        if delta < 0:
            if value < self._reset_share * self._value:
                delta, reset = max(value, 0.0), True
            else:  # correction or jitter: keep the higher baseline, nothing produced
                self._watch(seen)
                self._confirm(self._seen, seen)
                self._seen = max(self._seen, seen)
                return

        unwatched = when - self._seen > self._max_gap
        if unwatched:
            # Not watched in between: the energy fell somewhere since the last confirmation.
            # After a reset it counts from the reset, which a daily counter passes at night,
            # so it belongs to the latest minutes rather than to the whole gap.
            start = max(self._seen, when - self._max_step) if reset else self._seen
        else:
            start = max(self._changed, when - self._max_step)
        hours = (when - start).total_seconds() / 3600
        if self.max_power_kw is not None and delta > self.max_power_kw * max(hours, 1 / 60):
            self.rejected += 1
            self._value, self._changed, self._seen = value, when, max(self._seen, seen)
            return

        if unwatched:
            self._bins.add_energy(self._seen, start, 0.0, covered=False)
            self._bins.add_energy(start, when, delta, covered=False)
            self._resumed = when
        else:
            self._confirm(self._seen, start)
            resumed = self._resumed
            if resumed is not None and start < resumed < when:
                # Part of the spread lies in time that was not watched.
                share = delta * (resumed - start) / (when - start)
                self._bins.add_energy(start, resumed, share, covered=False)
                self._bins.add_energy(resumed, when, delta - share, covered=True)
            else:
                self._bins.add_energy(start, when, delta, covered=True)
        self._confirm(when, seen)
        self._value, self._changed, self._seen = value, when, max(self._seen, seen)

    def _watch(self, seen: datetime) -> None:
        """Note where watching resumes when the time since the last reading was a gap."""
        assert self._seen is not None
        if seen - self._seen > self._max_gap:
            self._resumed = seen

    def to_dict(self) -> dict[str, Any]:
        if self._value is None or self._changed is None or self._seen is None:
            return {}
        data: dict[str, Any] = {
            "value": self._value,
            "changed": self._changed.isoformat(),
            "seen": self._seen.isoformat(),
        }
        if self._resumed is not None:
            data["resumed"] = self._resumed.isoformat()
        return data

    def restore(self, data: dict[str, Any]) -> None:
        if data.get("changed") is not None:
            self._value = float(data["value"])
            self._changed = as_utc(datetime.fromisoformat(data["changed"]))
            self._seen = as_utc(datetime.fromisoformat(data["seen"]))
            if data.get("resumed"):
                self._resumed = as_utc(datetime.fromisoformat(data["resumed"]))
        elif data.get("time") is not None:  # state saved by 0.1.0
            self._value = float(data["value"])
            self._changed = self._seen = as_utc(datetime.fromisoformat(data["time"]))


class PowerMeter:
    """Integrates readings of a power sensor (kW) into bin energy.

    The last reading holds until the next one (like a state in Home Assistant). A gap
    longer than ``max_hold`` is left uncovered rather than guessed.
    """

    def __init__(self, bins: BinStore, *, max_hold: timedelta = timedelta(minutes=20)) -> None:
        self._bins = bins
        self._max_hold = max_hold
        self._last: tuple[datetime, float | None] | None = None

    @property
    def settled_until(self) -> datetime | None:
        """Energy is written up to the latest reading; nothing before it changes."""
        return None if self._last is None else self._last[0]

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
