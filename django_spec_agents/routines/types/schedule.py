from __future__ import annotations

from typing import TypeAlias

from django_spec_agents.routines.types.interval_schedule import IntervalSchedule
from django_spec_agents.routines.types.weekly_schedule import WeeklySchedule

Schedule: TypeAlias = WeeklySchedule | IntervalSchedule
