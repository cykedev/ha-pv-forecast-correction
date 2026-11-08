"""Time grid: forecast intervals, measurement bins and local days.

All instants are timezone-aware and handled in UTC. Days are local calendar days of a
given time zone, so a day has 23, 24 or 25 hours around daylight-saving transitions.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta, tzinfo

INTERVAL = timedelta(minutes=30)
"""Length of a forecast interval. Hourly inputs are split into two intervals."""

BIN = timedelta(minutes=5)
"""Length of a measurement bin. Actual energy is collected per bin."""

INTERVAL_HOURS = INTERVAL.total_seconds() / 3600
BIN_SECONDS = BIN.total_seconds()


def as_utc(value: datetime) -> datetime:
    """Return ``value`` in UTC; naive datetimes are rejected."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"naive datetime not allowed: {value!r}")
    return value.astimezone(UTC)


def floor_to(value: datetime, step: timedelta) -> datetime:
    """Floor an aware datetime to a multiple of ``step`` since the Unix epoch (UTC)."""
    seconds = int(step.total_seconds())
    epoch = int(as_utc(value).timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, UTC)


def local_day(value: datetime, tz: tzinfo) -> date:
    """Local calendar day of an instant."""
    return as_utc(value).astimezone(tz).date()


def local_time(day: date, wall: time, tz: tzinfo) -> datetime:
    """UTC instant of a local wall-clock time on ``day``.

    Non-existent times (spring forward) are shifted forward by the length of the gap,
    ambiguous times (fall back) resolve to their first occurrence.
    """
    naive = datetime.combine(day, wall)
    first = naive.replace(tzinfo=tz, fold=0)
    candidate = as_utc(first)
    if candidate.astimezone(tz).replace(tzinfo=None) != naive:
        # Inside a spring-forward gap: the wall time does not exist.
        later = as_utc(naive.replace(tzinfo=tz, fold=1))
        return max(candidate, later)
    return candidate


def day_bounds(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """UTC start (inclusive) and end (exclusive) of a local day."""
    return local_time(day, time(0), tz), local_time(day + timedelta(days=1), time(0), tz)


def steps(start: datetime, end: datetime, step: timedelta) -> Iterator[datetime]:
    """Grid points ``start, start + step, ...`` strictly before ``end``."""
    current = start
    while current < end:
        yield current
        current += step


def overlap_seconds(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> float:
    """Length in seconds of the intersection of two half-open ranges."""
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    return max(0.0, (end - start).total_seconds())


def hour_key(start: datetime, tz: tzinfo) -> datetime:
    """Start instant of the local clock hour that contains ``start``.

    Works for fall-back days (the repeated hour gets its own key) and for zones with
    half-hour offsets.
    """
    local = as_utc(start).astimezone(tz)
    return as_utc(start) - timedelta(minutes=local.minute, seconds=local.second)
