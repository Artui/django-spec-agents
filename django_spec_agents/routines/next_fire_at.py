from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from django_spec_agents.routines.types.interval_schedule import IntervalSchedule
from django_spec_agents.routines.types.schedule import Schedule


def next_fire_at(schedule: Schedule, timezone: ZoneInfo, after: datetime) -> datetime:
    """The first firing of `schedule` strictly after the aware `after`, in UTC.

    A weekly schedule reads its wall time in `timezone`: a time a DST change skips fires
    shifted by the gap, and a repeated time fires at its first occurrence only. An interval
    adds elapsed hours to `after`, so pass the firing just claimed, not the sweep's clock,
    or every late sweep pushes the cadence later. Raises `ValueError` for a naive `after`.
    """
    if after.tzinfo is None or after.utcoffset() is None:
        raise ValueError("next_fire_at needs an aware datetime.")
    instant = after.astimezone(UTC)
    if isinstance(schedule, IntervalSchedule):
        return instant + timedelta(hours=schedule.every_hours)
    today = instant.astimezone(timezone).date()
    days = (today + timedelta(days=offset) for offset in range(8))
    # fold=0, the default: a skipped time maps past the gap, a repeated one to its first instant.
    candidates = (
        datetime.combine(day, schedule.at, tzinfo=timezone).astimezone(UTC)
        for day in days
        if day.weekday() in schedule.weekdays
    )
    return next(candidate for candidate in candidates if candidate > instant)
