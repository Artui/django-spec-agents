from __future__ import annotations

from enum import StrEnum


class ThreadRunStatus(StrEnum):
    """Where a thread's runs stand, as a [`RunStore`][django_spec_agents.RunStore] reports it.

    ``running`` while a run holds the thread's claim under a live lease;
    ``waiting`` when the last run parked on deferred calls; ``failed`` when it
    raised or its lease lapsed; ``idle`` otherwise, a cancel or a timeout
    included.
    """

    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    FAILED = "failed"


__all__ = ["ThreadRunStatus"]
