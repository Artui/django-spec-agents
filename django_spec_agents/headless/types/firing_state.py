from __future__ import annotations

from enum import StrEnum


class FiringState(StrEnum):
    """Where a run is, as the queue that took it reports: a
    [`FiringQueue`][django_spec_agents.headless.types.firing_queue.FiringQueue].

    - ``pending``: it waits for a worker, or runs on one. In django-spec-tasks,
      ``PENDING`` and ``RUNNING``.
    - ``lapsed``: it ended without its body finishing, because its worker died, its
      principal lost the right to run it or its spec or binding is gone: ``ABANDONED``,
      ``AUTHORIZATION_LAPSED``, ``SPEC_UNAVAILABLE``.
    - ``finished``: it ended any other way, its answer or refusal kept: every other
      terminal state.
    """

    PENDING = "pending"
    LAPSED = "lapsed"
    FINISHED = "finished"


__all__ = ["FiringState"]
