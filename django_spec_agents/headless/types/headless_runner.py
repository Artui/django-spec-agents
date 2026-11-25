from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic_ai.capabilities import AbstractCapability

from django_spec_agents.headless.types.headless_start import HeadlessStart
from django_spec_agents.headless.types.run_stop import RunStop
from django_spec_agents.persistence.types.child_lineage_record import ChildLineageRecord
from django_spec_agents.persistence.types.thread_ref import ThreadRef


@runtime_checkable
class HeadlessRunner(Protocol):
    """Starts an agent run on the server, with no browser and no stream.

    A transport implements it over its mounts; a routine's firing or a parent
    run's child calls it. ``agent_id`` names the mount. ``prompt`` is the run's
    only new user message: prior history comes from the server, and a headless
    run carries no client tools.

    **The principal is a user, never anonymous.** The run's owner is
    ``str(principal.pk)``, the id every owner-scoped store already resolves from
    an authenticated request; an anonymous principal is refused, since its
    bucket needs a browser session. The caller authorizes the run, not the
    transport: a routine by its own permission check, a child by its parent's
    run.

    **Every headless run has a deadline.** ``timeout`` is passed to the run as
    pydantic-ai's ``timeout=``, in seconds; a run past it ends ``RunTimedOut``,
    which the runner records as ``RunOutcome.TIMED_OUT`` with the partial kept.

    ``capabilities`` are composed into the run beside the mount's own, such as
    a guard that pre-approves a routine's tools. ``parent`` names the thread a
    child run belongs to, with its scope, since a child often runs on another agent.
    ``lineage`` is the bounds the child runs under: the runner saves both on the child's
    conversation and composes
    [`child_lineage_for`][django_spec_agents.child_lineage_for] from them on every run of
    its thread, the first included, since capabilities cannot cross a queue; a child's
    start passes no ``capabilities``.
    ``run_id`` on ``run_to_first_stop`` adopts the run already claimed under this id, as a
    queued start claims its thread before a worker runs it; without one the call claims.

    ``answers`` resumes a thread parked on a question: it maps each waiting call's
    ``tool_call_id`` to its answer's text. A runner given it must resume the thread's stored
    history with ``DeferredToolResults(calls=answers)`` and send no new user message, so
    ``prompt`` is not sent; pydantic-ai refuses a new prompt while the history holds the
    unanswered call. Callers pass it only when set, so a runner without it serves every
    other run. A message can land after a run's last read of its queue, and an answer
    before the asking run parks; so after recording any run's end with ``RunStore.finish``,
    a runner calls [`wake_thread`][django_spec_agents.child_sessions.wake_thread.wake_thread]
    for its thread once, which starts or resumes it when messages are still queued.

    ``prompt_metadata`` marks a ``prompt`` no user wrote: a runner keeps it as the metadata
    of the user message it stores for the prompt, and never sends it to the model. A wake of
    an idle thread passes it, so a runner whose thread is woken must take it; every other
    caller leaves it out. A queued start carries it in its ``HeadlessFiring``.

    ``parent_run_id`` is the run this one comes from: the parent run that started a child,
    or the parked run a resume answers; the runner reports it as the run's
    ``RUN_STARTED.parentRunId``. ``routine_id`` is the routine a firing runs; the runner
    saves it on the thread's conversation, as it saves ``parent``, so every firing's
    thread names its routine. Every caller in this package passes both on every call, so
    a runner must accept them.
    """

    async def start(
        self,
        agent_id: str,
        prompt: str,
        principal: Any,
        *,
        timeout: float,
        thread_id: str | None = None,
        parent: ThreadRef | None = None,
        lineage: ChildLineageRecord | None = None,
        capabilities: Sequence[AbstractCapability[Any]] = (),
        answers: Mapping[str, str] | None = None,
        parent_run_id: str | None = None,
        routine_id: str | None = None,
        prompt_metadata: Mapping[str, str] | None = None,
    ) -> HeadlessStart:
        """Start the run and return once it has claimed its thread; it goes on in the background."""
        ...

    async def run_to_first_stop(
        self,
        agent_id: str,
        prompt: str,
        principal: Any,
        *,
        timeout: float,
        thread_id: str | None = None,
        parent: ThreadRef | None = None,
        lineage: ChildLineageRecord | None = None,
        capabilities: Sequence[AbstractCapability[Any]] = (),
        run_id: str | None = None,
        answers: Mapping[str, str] | None = None,
        parent_run_id: str | None = None,
        routine_id: str | None = None,
        prompt_metadata: Mapping[str, str] | None = None,
    ) -> RunStop:
        """Run until the run ends or parks, for a caller that waits on the answer."""
        ...

    async def cancel(self, thread_id: str, principal: Any) -> bool:
        """Record a cancel for the thread's live run; ``False`` if no run holds it."""
        ...


__all__ = ["HeadlessRunner"]
