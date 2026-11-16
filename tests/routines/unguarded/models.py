from __future__ import annotations

from django_spec_agents.routines import AbstractRoutine


class TenantRoutineBase(AbstractRoutine):
    """An abstract layer with no policy: never installed as a table, so never checked."""

    class Meta:
        abstract = True


class UnguardedRoutine(TenantRoutineBase):
    """Installed with no policy: the system check refuses it."""
