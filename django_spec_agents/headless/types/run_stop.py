from __future__ import annotations

from dataclasses import dataclass

from django_spec_agents.persistence.types.run_outcome import RunOutcome
from django_spec_agents.persistence.types.run_waiting import RunWaiting


@dataclass(frozen=True)
class RunStop:
    """Where a headless run first stopped: it ended, or it parked on a deferred call.

    ``waiting`` holds every call it parked on, non-empty exactly when ``outcome`` is
    ``RunOutcome.WAITING``.
    """

    thread_id: str
    run_id: str
    outcome: RunOutcome
    waiting: tuple[RunWaiting, ...] = ()

    def __post_init__(self) -> None:
        if (self.outcome is RunOutcome.WAITING) != bool(self.waiting):
            raise ValueError("waiting is non-empty exactly when the outcome is WAITING")


__all__ = ["RunStop"]
