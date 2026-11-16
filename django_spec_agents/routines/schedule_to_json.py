from __future__ import annotations

from typing import Any

from django_spec_agents.routines.types.schedule import Schedule
from django_spec_agents.routines.types.weekly_schedule import WeeklySchedule


def schedule_to_json(schedule: Schedule) -> dict[str, Any]:
    """`schedule` as the JSON a routine stores; `parse_schedule` reads it back to an equal one."""
    if isinstance(schedule, WeeklySchedule):
        return {
            "kind": "weekly",
            "weekdays": sorted(schedule.weekdays),
            "at": schedule.at.strftime("%H:%M"),
        }
    return {"kind": "interval", "every_hours": schedule.every_hours}
