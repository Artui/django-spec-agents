from __future__ import annotations

from dataclasses import dataclass

from django_spec_agents.persistence.types.run_end_reason import RunEndReason
from django_spec_agents.persistence.types.run_waiting import RunWaiting
from django_spec_agents.persistence.types.thread_ref import ThreadRef
from django_spec_agents.persistence.types.thread_run_status import ThreadRunStatus


@dataclass(frozen=True)
class ChildSession:
    """A child session as its parent sees it: the child's thread and where its runs stand.

    A child is live while it runs or waits, since a parked child still holds its slot;
    ``reason`` says why its last run ended early: timed out, raised or interrupted.
    """

    thread: ThreadRef
    status: ThreadRunStatus
    waiting: RunWaiting | None = None
    reason: RunEndReason | None = None

    @property
    def live(self) -> bool:
        """Whether the child counts against the running limits: it runs, or it waits."""
        return self.status in (ThreadRunStatus.RUNNING, ThreadRunStatus.WAITING)


__all__ = ["ChildSession"]
