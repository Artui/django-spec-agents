from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from django.contrib.auth.base_user import AbstractBaseUser

from django_spec_agents.headless.types.firing_state import FiringState
from django_spec_agents.routines import IntervalSchedule, WeeklySchedule, schedule_to_json
from tests.routines.testapp.models import TeamRoutine

WEEKDAYS_AT_8 = WeeklySchedule(weekdays=frozenset(range(5)), at=datetime(1, 1, 1, 8).time())
EVERY_6_HOURS = IntervalSchedule(every_hours=6)


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


@dataclass
class Enqueued:
    spec_name: str
    params: dict[str, Any]
    actor: Any


@dataclass
class RecordingQueue:
    """A FiringQueue that records each enqueue and answers `state` from `states`."""

    states: dict[str, FiringState] = field(default_factory=dict)
    enqueued: list[Enqueued] = field(default_factory=list)
    refuse: bool = False

    def enqueue(self, spec_name: str, params: Mapping[str, Any], *, actor: Any) -> str:
        if self.refuse:
            raise RuntimeError("the queue is down")
        self.enqueued.append(Enqueued(spec_name, dict(params), actor))
        return f"run-{len(self.enqueued)}"

    def state(self, run_id: str) -> FiringState | None:
        return self.states.get(run_id)


def routine(principal: AbstractBaseUser, due: datetime | None, **changes: Any) -> TeamRoutine:
    schedule = changes.pop("schedule", WEEKDAYS_AT_8)
    fields: dict[str, Any] = {
        "name": "Overdue digest",
        "agent_id": "assistant",
        "prompt": "tell me what is overdue",
        "schedule": schedule_to_json(schedule),
        "timezone": "UTC",
        "principal": principal,
        "team": "core",
        "next_fire_at": due,
    }
    return TeamRoutine.objects.create(**{**fields, **changes})
