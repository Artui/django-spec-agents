from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django_spec_agents.persistence.types.run_end_reason import RunEndReason
from django_spec_agents.persistence.types.run_waiting import RunWaiting
from django_spec_agents.persistence.types.thread_run_status import ThreadRunStatus


@dataclass(frozen=True)
class ThreadRunState:
    """One thread's run status, as a [`RunStore`][django_spec_agents.RunStore] reads it.

    ``active_run_id`` is the run holding the claim, ``None`` once it ends or
    its lease lapses.
    ``last_run_id`` outlives it: a parked run's id is what a resume answers
    to, while the resumed run claims a fresh id. A ``running`` thread whose
    lease has lapsed reads as ``failed`` with reason ``interrupted``.
    ``cancel_requested`` is set by a cancel the run's owner has yet to act on.
    ``last_read_at`` is when a reader last marked the thread read, ``None`` if
    never.
    """

    thread_id: str
    status: ThreadRunStatus
    reason: RunEndReason | None = None
    waiting: RunWaiting | None = None
    active_run_id: str | None = None
    last_run_id: str | None = None
    lease_expires_at: datetime | None = None
    cancel_requested: bool = False
    last_read_at: datetime | None = None


__all__ = ["ThreadRunState"]
