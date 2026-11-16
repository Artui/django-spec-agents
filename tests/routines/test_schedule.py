from __future__ import annotations

from datetime import UTC, time, timedelta, timezone

import pytest

from django_spec_agents.routines import (
    MAX_ROUTINE_INTERVAL,
    MIN_ROUTINE_INTERVAL,
    IntervalSchedule,
    InvalidRoutineError,
    WeeklySchedule,
    parse_schedule,
    schedule_to_json,
)


@pytest.mark.parametrize(
    "schedule",
    [
        WeeklySchedule(weekdays=frozenset({4, 0, 2}), at=time(7, 5)),
        IntervalSchedule(every_hours=6),
    ],
)
def test_a_schedule_round_trips_through_its_json(
    schedule: WeeklySchedule | IntervalSchedule,
) -> None:
    assert parse_schedule(schedule_to_json(schedule)) == schedule


def test_the_weekly_json_is_sorted_and_minute_precise() -> None:
    schedule = WeeklySchedule(weekdays=frozenset({4, 0}), at=time(7, 5))

    assert schedule_to_json(schedule) == {"kind": "weekly", "weekdays": [0, 4], "at": "07:05"}


def test_the_minimum_interval_is_an_hour() -> None:
    assert timedelta(hours=1) == MIN_ROUTINE_INTERVAL
    assert IntervalSchedule(every_hours=1).every_hours == 1


def test_the_maximum_interval_is_366_days() -> None:
    assert timedelta(days=366) == MAX_ROUTINE_INTERVAL
    assert IntervalSchedule(every_hours=366 * 24).every_hours == 366 * 24


@pytest.mark.parametrize("hours", [0, -3, True, 1.5, "2", 366 * 24 + 1, 10**12])
def test_an_interval_outside_the_bounds_or_not_whole_hours_is_refused(hours: object) -> None:
    with pytest.raises(InvalidRoutineError, match="whole number of hours"):
        IntervalSchedule(every_hours=hours)


@pytest.mark.parametrize(
    ("weekdays", "at", "message"),
    [
        (frozenset(), time(8, 0), "at least one weekday"),
        (frozenset({7}), time(8, 0), "0 \\(Monday\\) to 6"),
        (frozenset({-1}), time(8, 0), "0 \\(Monday\\) to 6"),
        (frozenset({True}), time(8, 0), "0 \\(Monday\\) to 6"),
        (frozenset({0}), time(8, 0, 30), "to the minute"),
        (frozenset({0}), time(8, 0, 0, 5), "to the minute"),
        (frozenset({0}), time(8, 0, tzinfo=UTC), "naive wall time"),
        (frozenset({0}), time(8, 0, tzinfo=timezone(timedelta(hours=2))), "naive wall time"),
    ],
)
def test_a_weekly_schedule_outside_the_vocabulary_is_refused(
    weekdays: frozenset[int], at: time, message: str
) -> None:
    with pytest.raises(InvalidRoutineError, match=message):
        WeeklySchedule(weekdays=weekdays, at=at)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ("0 8 * * 1-5", "a JSON object"),
        ({"kind": "cron", "expression": "0 8 * * *"}, "nothing else"),
        ({"kind": "weekly", "weekdays": [0], "at": "08:00", "extra": 1}, "nothing else"),
        ({"kind": "interval", "every_hours": 2, "at": "08:00"}, "nothing else"),
        ({"kind": "weekly", "weekdays": [0]}, "nothing else"),
        ({"kind": "weekly", "weekdays": "01234", "at": "08:00"}, "without repeats"),
        ({"kind": "weekly", "weekdays": [0, 0], "at": "08:00"}, "without repeats"),
        ({"kind": "weekly", "weekdays": [[0]], "at": "08:00"}, "without repeats"),
        ({"kind": "weekly", "weekdays": [{}], "at": "08:00"}, "without repeats"),
        ({"kind": "weekly", "weekdays": [0], "at": "8:00"}, "'HH:MM'"),
        ({"kind": "weekly", "weekdays": [0], "at": "08:00:00"}, "'HH:MM'"),
        ({"kind": "weekly", "weekdays": [0], "at": "25:00"}, "'HH:MM'"),
        ({"kind": "weekly", "weekdays": [0], "at": "08-00"}, "'HH:MM'"),
        ({"kind": "weekly", "weekdays": [0], "at": 800}, "'HH:MM'"),
        ({"kind": "weekly", "weekdays": [9], "at": "08:00"}, "0 \\(Monday\\) to 6"),
        ({"kind": "interval", "every_hours": 0}, "whole number of hours"),
    ],
)
def test_a_schedule_outside_the_vocabulary_does_not_parse(data: object, message: str) -> None:
    with pytest.raises(InvalidRoutineError, match=message):
        parse_schedule(data)
