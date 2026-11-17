from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from django_spec_agents.headless.types.firing_state import FiringState


@runtime_checkable
class FiringQueue(Protocol):
    """Runs a declared kernel spec on a worker, outside the process that asked.

    The host fills it, from django-spec-tasks' ``enqueue`` and ``state_of`` over a
    binding whose registry holds the specs it may be asked for, so this package
    takes no dependency on a queue. A detached or headless start enqueues
    [`run_headless`][django_spec_agents.headless.run_headless.run_headless], and a
    routine's firing its own spec, through the same queue.

    Both methods are synchronous, as the queue's are: a caller enqueues inside its
    own transaction, and the queue sends the run when that transaction commits.
    """

    def enqueue(self, spec_name: str, params: Mapping[str, Any], *, actor: Any) -> str:
        """Check the call now, record it, and send it on commit; the run's id.

        ``actor`` is the principal the worker runs the spec as, loaded again by
        its primary key there, so it is required and never anonymous. What the
        spec refuses at the call (its permissions, its arguments) is raised here
        and nothing is recorded.
        """
        ...

    def state(self, run_id: str) -> FiringState | None:
        """Where the run is; ``None`` for an id the queue does not hold, or no longer does."""
        ...


__all__ = ["FiringQueue"]
