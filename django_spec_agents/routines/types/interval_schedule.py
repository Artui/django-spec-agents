from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django_spec_agents.routines.invalid_routine_error import InvalidRoutineError
from django_spec_agents.routines.types.min_routine_interval import MIN_ROUTINE_INTERVAL


@dataclass(frozen=True)
class IntervalSchedule:
    """Fire every `every_hours` hours of elapsed time, whatever the timezone's DST changes.

    Raises `InvalidRoutineError` for an interval shorter than `MIN_ROUTINE_INTERVAL`.
    """

    every_hours: int

    def __post_init__(self) -> None:
        if (
            type(self.every_hours) is not int
            or timedelta(hours=self.every_hours) < MIN_ROUTINE_INTERVAL
        ):
            raise InvalidRoutineError(
                f"An interval is a whole number of hours, at least {MIN_ROUTINE_INTERVAL}."
            )
