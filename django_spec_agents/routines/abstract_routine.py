from __future__ import annotations

from typing import Any, Self
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import models

from django_spec_agents.routines.parse_schedule import parse_schedule
from django_spec_agents.routines.types.max_routine_agent_id_length import (
    MAX_ROUTINE_AGENT_ID_LENGTH,
)
from django_spec_agents.routines.types.max_routine_name_length import MAX_ROUTINE_NAME_LENGTH
from django_spec_agents.routines.types.preapproved_tool import PreapprovedTool
from django_spec_agents.routines.types.routine_policy import RoutinePolicy
from django_spec_agents.routines.types.routine_sweep import RoutineSweep
from django_spec_agents.routines.types.schedule import Schedule


class AbstractRoutine(models.Model):
    """A stored prompt for one agent, its schedule and its principal, the user who created it.

    The consumer subclasses it with its tenant relation and returns its policy from
    `routine_policy()`. `next_fire_at` is `None` while the routine is disabled.
    """

    name = models.CharField(max_length=MAX_ROUTINE_NAME_LENGTH)
    agent_id = models.CharField(max_length=MAX_ROUTINE_AGENT_ID_LENGTH)
    prompt = models.TextField()
    schedule = models.JSONField()
    timezone = models.CharField(max_length=64)
    principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    enabled = models.BooleanField(default=True)
    paused_reason = models.TextField(blank=True, default="")
    preapproved_tools = models.JSONField(blank=True, default=list)
    next_fire_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_run_id = models.CharField(max_length=255, null=True, blank=True, default=None)
    last_thread_id = models.CharField(max_length=255, null=True, blank=True, default=None)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def get_schedule(self) -> Schedule:
        """The stored schedule, read through `parse_schedule`."""
        return parse_schedule(self.schedule)

    def get_timezone(self) -> ZoneInfo:
        """The IANA zone the schedule's wall times are read in."""
        return ZoneInfo(self.timezone)

    def get_preapproved_tools(self) -> tuple[PreapprovedTool, ...]:
        """The granted tools, each with the destructive flag pinned when it was granted."""
        return tuple(
            PreapprovedTool(name=entry["name"], destructive=entry["destructive"])
            for entry in self.preapproved_tools
        )

    @classmethod
    def routine_policy(cls) -> RoutinePolicy[Self] | None:
        """The policy every read, write and firing goes through; `None` fails the system check."""
        return None

    @classmethod
    def routine_sweep(cls) -> RoutineSweep | None:
        """The queue and spec `run_due_routines` fires this model with; `None` leaves it unswept."""
        return None

    @classmethod
    def thread_waits(cls, thread_id: str, principal: Any) -> bool:
        """Whether the last firing's thread still waits on a person; the host reads its run store.

        Return `True` while `principal`'s thread `thread_id` is parked on an approval or a
        question, and the sweep skips the next firing rather than start another beside it. The
        default, `False`, never skips, so a host without a run store fires on schedule.
        """
        return False
