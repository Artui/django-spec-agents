from __future__ import annotations

from typing import TypeVar

from django_spec_agents.routines.abstract_routine import AbstractRoutine

RoutineT = TypeVar("RoutineT", bound=AbstractRoutine)
