"""Run the correction engine over archived data, exactly as it would have run live."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from .archive import MeasuredBin, Snapshot
from .engine import Corrector, Settings
from .level import DayStats
from .score import DayReport
from .timeline import BIN, as_utc, day_bounds, local_day

UPDATE_OFFSET = timedelta(seconds=5)
"""The integration updates a few seconds after each 5-minute mark."""


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Reports and day statistics of one replay run."""

    settings: Settings
    reports: dict[date, DayReport]
    stats: dict[date, DayStats]


def replay(
    snapshots: Sequence[Snapshot],
    bins: Sequence[MeasuredBin],
    settings: Settings,
    tz: tzinfo,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    step: timedelta = BIN,
    offset: timedelta = UPDATE_OFFSET,
) -> ReplayResult:
    """Feed snapshots and bins in time order and update the engine every ``step``.

    A snapshot becomes visible at its capture time and triggers an update then, like a
    state change does live; a bin becomes visible once it has ended. Regular updates run
    at ``offset`` past each step, like the integration's timer. The run starts at local
    midnight of the first day with data and stops after the last bin.
    """
    ordered_snapshots = sorted(snapshots, key=lambda s: s.captured_at)
    ordered_bins = sorted(bins, key=lambda b: b.start)
    if not ordered_snapshots or not ordered_bins:
        return ReplayResult(settings, {}, {})

    first = start or min(ordered_snapshots[0].captured_at, ordered_bins[0].start)
    current, _ = day_bounds(local_day(first, tz), tz)
    current += offset
    stop = as_utc(end) if end else ordered_bins[-1].start + BIN
    # Run one step into the next day so the last day is rolled over and reported.
    _, stop = day_bounds(local_day(stop - timedelta(seconds=1), tz), tz)
    stop += offset

    engine = Corrector(settings, tz)
    reports: dict[date, DayReport] = {}
    stats: dict[date, DayStats] = {}
    bin_index = 0

    def feed_bins(until: datetime) -> None:
        nonlocal bin_index
        while bin_index < len(ordered_bins) and ordered_bins[bin_index].start + BIN <= until:
            item = ordered_bins[bin_index]
            engine.set_bin(item.start, item.energy_kwh, item.covered_s, item.curtailed, item.gap_s)
            bin_index += 1

    collected: list[DayReport | None] = [None]

    def collect() -> None:
        report = engine.report
        if report is not None and report is not collected[0]:
            collected[0] = report
            reports[report.day] = report
            if report.day in engine.history:
                stats[report.day] = engine.history[report.day]

    def advance(when: datetime) -> None:
        engine.update(when)
        collect()

    snap_index = 0
    while current <= stop:
        # Live, a changed base forecast triggers an update right away; do the same here.
        while (
            snap_index < len(ordered_snapshots)
            and ordered_snapshots[snap_index].captured_at <= current
        ):
            snapshot = ordered_snapshots[snap_index]
            feed_bins(snapshot.captured_at)
            engine.set_base(snapshot.forecast, snapshot.captured_at)
            collect()  # a new base closes the previous day first
            if engine.last_update is None or snapshot.captured_at > engine.last_update:
                advance(snapshot.captured_at)
            snap_index += 1
        feed_bins(current)
        advance(current)
        current += step
    return ReplayResult(settings, reports, stats)
