from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from django_spec_agents.routines.invalid_routine_error import InvalidRoutineError


@dataclass(frozen=True)
class WeeklySchedule:
    """Fire on each of `weekdays` (0 is Monday, 6 is Sunday) at the local wall time `at`.

    `at` is a naive time to the minute, read in the routine's timezone. A time a DST change
    skips fires at the same instant shifted by the gap, and a time it repeats fires once, at
    its first occurrence. Raises `InvalidRoutineError` for no weekday, a weekday outside 0 to
    6, an aware `at`, or one with seconds.
    """

    weekdays: frozenset[int]
    at: time

    def __post_init__(self) -> None:
        if not self.weekdays:
            raise InvalidRoutineError("A weekly schedule needs at least one weekday.")
        if not all(type(day) is int and 0 <= day <= 6 for day in self.weekdays):
            raise InvalidRoutineError("Weekdays are integers from 0 (Monday) to 6 (Sunday).")
        if self.at.tzinfo is not None or self.at.second or self.at.microsecond:
            raise InvalidRoutineError(
                "A weekly schedule's time is a naive wall time to the minute."
            )
