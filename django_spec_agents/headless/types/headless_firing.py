from __future__ import annotations

from dataclasses import dataclass

from django_spec_agents.persistence.types.child_lineage_record import ChildLineageRecord
from django_spec_agents.persistence.types.thread_ref import ThreadRef


@dataclass(frozen=True)
class HeadlessFiring:
    """The arguments [`run_headless`][django_spec_agents.headless.run_headless.run_headless] takes.

    They are those of
    [`HeadlessRunner.run_to_first_stop`][django_spec_agents.HeadlessRunner.run_to_first_stop]
    that cross a queue as JSON. ``capabilities`` cannot, so a queued run carries none.
    ``run_id`` adopts the run already claimed under this id, as a queued start claims its
    thread before the worker takes the firing up. ``answers`` resumes a thread parked on a
    question, a ``tool_call_id`` to its answer's text; the run sends no new ``prompt``.
    ``inbox_mark`` is the thread's inbox mark taken when the firing was enqueued, for the
    worker to wake on mail newer than it; ``None`` for a firing that carries none.
    ``parent_run_id`` and ``routine_id`` are the runner's own, the run and the routine the
    firing comes from.
    """

    agent_id: str
    prompt: str
    timeout: float
    thread_id: str | None = None
    parent: ThreadRef | None = None
    lineage: ChildLineageRecord | None = None
    run_id: str | None = None
    answers: dict[str, str] | None = None
    inbox_mark: int | None = None
    parent_run_id: str | None = None
    routine_id: str | None = None


__all__ = ["HeadlessFiring"]
