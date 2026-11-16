from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

import pytest

from django_spec_agents.routines import IntervalSchedule, WeeklySchedule, next_fire_at

NEW_YORK = ZoneInfo("America/New_York")
WEEKDAYS = frozenset(range(5))
DAILY = frozenset(range(7))


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def local(*parts: int, fold: int = 0) -> datetime:
    return datetime(*parts, tzinfo=NEW_YORK, fold=fold)


def test_a_weekday_routine_fires_later_the_same_day() -> None:
    schedule = WeeklySchedule(weekdays=WEEKDAYS, at=time(8, 0))

    # 2026-11-16 is a Monday.
    fired = next_fire_at(schedule, NEW_YORK, local(2026, 11, 16, 7, 59))

    assert fired == local(2026, 11, 16, 8, 0)
    assert fired.tzinfo is UTC


def test_a_time_already_passed_today_fires_on_the_next_weekday() -> None:
    schedule = WeeklySchedule(weekdays=WEEKDAYS, at=time(8, 0))

    assert next_fire_at(schedule, NEW_YORK, local(2026, 11, 16, 8, 1)) == local(2026, 11, 17, 8, 0)


def test_a_firing_is_strictly_after_the_given_instant() -> None:
    schedule = WeeklySchedule(weekdays=WEEKDAYS, at=time(8, 0))

    assert next_fire_at(schedule, NEW_YORK, local(2026, 11, 16, 8, 0)) == local(2026, 11, 17, 8, 0)


def test_friday_after_the_time_wraps_past_the_weekend_to_monday() -> None:
    schedule = WeeklySchedule(weekdays=WEEKDAYS, at=time(8, 0))

    assert next_fire_at(schedule, NEW_YORK, local(2026, 11, 20, 9, 0)) == local(2026, 11, 23, 8, 0)


def test_saturday_fires_on_monday() -> None:
    schedule = WeeklySchedule(weekdays=WEEKDAYS, at=time(8, 0))

    assert next_fire_at(schedule, NEW_YORK, local(2026, 11, 21, 6, 0)) == local(2026, 11, 23, 8, 0)


def test_a_single_weekday_after_its_time_fires_a_week_later() -> None:
    schedule = WeeklySchedule(weekdays=frozenset({0}), at=time(8, 0))

    assert next_fire_at(schedule, NEW_YORK, local(2026, 11, 16, 9, 0)) == local(2026, 11, 23, 8, 0)


def test_the_weekday_is_the_local_one_not_utcs() -> None:
    schedule = WeeklySchedule(weekdays=frozenset({0}), at=time(22, 0))

    # Monday 22:00 in New York is Tuesday 03:00 UTC; from Monday noon UTC it is that evening.
    assert next_fire_at(schedule, NEW_YORK, utc(2026, 11, 16, 12, 0)) == utc(2026, 11, 17, 3, 0)


def test_the_wall_time_holds_across_a_dst_change() -> None:
    schedule = WeeklySchedule(weekdays=DAILY, at=time(8, 0))

    # 2026-03-08 springs forward: 8:00 is 13:00 UTC the day before, 12:00 UTC on the day.
    assert next_fire_at(schedule, NEW_YORK, local(2026, 3, 7, 9, 0)) == utc(2026, 3, 8, 12, 0)
    assert next_fire_at(schedule, NEW_YORK, local(2026, 3, 6, 9, 0)) == utc(2026, 3, 7, 13, 0)


def test_a_skipped_time_fires_shifted_by_the_gap() -> None:
    schedule = WeeklySchedule(weekdays=DAILY, at=time(2, 30))

    fired = next_fire_at(schedule, NEW_YORK, local(2026, 3, 8, 0, 0))

    assert fired == utc(2026, 3, 8, 7, 30)
    assert fired.astimezone(NEW_YORK).time() == time(3, 30)
    assert next_fire_at(schedule, NEW_YORK, fired) == local(2026, 3, 9, 2, 30)


def test_a_repeated_time_fires_once_at_its_first_occurrence() -> None:
    schedule = WeeklySchedule(weekdays=DAILY, at=time(1, 30))

    # 2026-11-01 falls back: 1:30 happens at 05:30 UTC (EDT) and again at 06:30 UTC (EST).
    first = next_fire_at(schedule, NEW_YORK, local(2026, 11, 1, 0, 0))

    assert first == utc(2026, 11, 1, 5, 30)
    assert next_fire_at(schedule, NEW_YORK, first) == local(2026, 11, 2, 1, 30)
    assert next_fire_at(schedule, NEW_YORK, utc(2026, 11, 1, 6, 0)) == local(2026, 11, 2, 1, 30)


def test_an_interval_adds_elapsed_hours_across_a_dst_change() -> None:
    schedule = IntervalSchedule(every_hours=6)

    fired = next_fire_at(schedule, NEW_YORK, local(2026, 3, 8, 0, 0))

    assert fired - local(2026, 3, 8, 0, 0) == timedelta(hours=6)
    assert fired.astimezone(NEW_YORK).hour == 7
    assert fired.tzinfo is UTC


def test_a_naive_instant_is_refused() -> None:
    with pytest.raises(ValueError, match="aware"):
        next_fire_at(IntervalSchedule(every_hours=1), NEW_YORK, datetime(2026, 1, 1))


class _NoOffset(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None


def test_an_instant_whose_zone_gives_no_offset_is_refused() -> None:
    with pytest.raises(ValueError, match="aware"):
        next_fire_at(
            IntervalSchedule(every_hours=1), NEW_YORK, datetime(2026, 1, 1, tzinfo=_NoOffset())
        )
