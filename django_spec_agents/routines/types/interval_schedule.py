from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django_spec_agents.routines.invalid_routine_error import InvalidRoutineError
from django_spec_agents.routines.types.max_routine_interval import MAX_ROUTINE_INTERVAL
from django_spec_agents.routines.types.min_routine_interval import MIN_ROUTINE_INTERVAL

_HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class IntervalSchedule:
    """Fire every `every_hours` hours of elapsed time, whatever the timezone's DST changes.

    Raises `InvalidRoutineError` for an interval shorter than `MIN_ROUTINE_INTERVAL` or
    longer than `MAX_ROUTINE_INTERVAL`.
    """

    every_hours: int

    def __post_init__(self) -> None:
        if type(self.every_hours) is not int or not (
            MIN_ROUTINE_INTERVAL // _HOUR <= self.every_hours <= MAX_ROUTINE_INTERVAL // _HOUR
        ):
            raise InvalidRoutineError(
                f"An interval is a whole number of hours, from {MIN_ROUTINE_INTERVAL} to "
                f"{MAX_ROUTINE_INTERVAL}."
            )
