from __future__ import annotations

from django.http import HttpRequest

from django_spec_agents.child_sessions.types.inbound_queue import InboundQueue
from django_spec_agents.headless.types.headless_runner import HeadlessRunner
from django_spec_agents.headless.types.headless_start import HeadlessStart
from django_spec_agents.persistence.types.run_store import RunStore
from django_spec_agents.persistence.types.thread_ref import ThreadRef
from django_spec_agents.persistence.types.thread_run_status import ThreadRunStatus

_WAKE_PROMPT = "New messages for this conversation arrived while it was idle."
_BUSY = (ThreadRunStatus.RUNNING, ThreadRunStatus.WAITING)


async def wake_thread(
    thread: ThreadRef,
    *,
    request: HttpRequest,
    runner: HeadlessRunner,
    runs: RunStore,
    queue: InboundQueue,
    timeout: float,
    prompt: str = _WAKE_PROMPT,
) -> HeadlessStart | None:
    """Start a headless run on ``thread`` if it is idle and its inbound queue holds messages.

    Call it after enqueueing for ``thread``. By the thread's state in ``runs``: running, it
    does nothing, since the run reads the queue at its next model request; waiting on a
    deferred call, it does nothing, since a second run must not start before the resume, which
    reads the queue; idle, failed or never run, with messages pending, it starts one run on the
    thread's scope as ``request.user``, with ``prompt`` as its new message (by default a
    line saying messages arrived), and returns it. The run's mount composes
    [`DrainInbound`][django_spec_agents.child_sessions.drain_inbound.DrainInbound] to read them.

    It passes the runner no ``lineage`` or ``capabilities``: a child's thread keeps its stored
    lineage, which the runner composes on every run. When two wakes race, the runner's claim
    lets one run start; the other's refusal is absorbed once the thread reads as running.
    """
    if thread.scope is None:
        raise ValueError(f"waking thread {thread.thread_id!r} needs its scope, the agent id")
    state = await runs.get(thread.thread_id, request=request)
    if state is not None and state.status in _BUSY:
        return None
    if not await queue.pending(thread, request=request, limit=1):
        return None
    try:
        return await runner.start(
            thread.scope, prompt, request.user, timeout=timeout, thread_id=thread.thread_id
        )
    except Exception:
        state = await runs.get(thread.thread_id, request=request)
        if state is not None and state.status is ThreadRunStatus.RUNNING:
            return None
        raise


__all__ = ["wake_thread"]
