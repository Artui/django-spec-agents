from __future__ import annotations

from collections.abc import Sequence
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
    conversation, so a later run of its thread can compose
    [`child_lineage_for`][django_spec_agents.child_lineage_for].
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
    ) -> RunStop:
        """Run until the run ends or parks, for a caller that waits on the answer."""
        ...

    async def cancel(self, thread_id: str, principal: Any) -> bool:
        """Record a cancel for the thread's live run; ``False`` if no run holds it."""
        ...


__all__ = ["HeadlessRunner"]
