from __future__ import annotations

from datetime import time

from django_spec_agents.routines.invalid_routine_error import InvalidRoutineError
from django_spec_agents.routines.types.interval_schedule import IntervalSchedule
from django_spec_agents.routines.types.schedule import Schedule
from django_spec_agents.routines.types.weekly_schedule import WeeklySchedule


def _time(value: object) -> time:
    """An `"HH:MM"` string as a naive time."""
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise InvalidRoutineError("A weekly schedule's time is an 'HH:MM' string.")
    try:
        return time.fromisoformat(value)
    except ValueError as error:
        raise InvalidRoutineError("A weekly schedule's time is an 'HH:MM' string.") from error


def parse_schedule(data: object) -> Schedule:
    """Read a stored or submitted schedule; raises `InvalidRoutineError` outside the vocabulary.

    The vocabulary is closed: `{"kind": "weekly", "weekdays": [0, ...], "at": "HH:MM"}` or
    `{"kind": "interval", "every_hours": N}`, and no other key. There are no cron strings.
    """
    if not isinstance(data, dict):
        raise InvalidRoutineError("A schedule is a JSON object.")
    kind = data.get("kind")
    if kind == "weekly" and set(data) == {"kind", "weekdays", "at"}:
        weekdays = data["weekdays"]
        if not isinstance(weekdays, list) or len(set(weekdays)) != len(weekdays):
            raise InvalidRoutineError("A weekly schedule's weekdays are a list without repeats.")
        return WeeklySchedule(weekdays=frozenset(weekdays), at=_time(data["at"]))
    if kind == "interval" and set(data) == {"kind", "every_hours"}:
        return IntervalSchedule(every_hours=data["every_hours"])
    raise InvalidRoutineError(
        "A schedule is {'kind': 'weekly', 'weekdays', 'at'} or {'kind': 'interval', "
        "'every_hours'}, and nothing else."
    )
