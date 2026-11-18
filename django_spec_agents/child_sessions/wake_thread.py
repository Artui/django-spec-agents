from __future__ import annotations

from django.http import HttpRequest

from django_spec_agents.child_sessions.types.inbound_queue import InboundQueue
from django_spec_agents.headless.types.headless_runner import HeadlessRunner
from django_spec_agents.headless.types.headless_start import HeadlessStart
from django_spec_agents.persistence.types.conversation_store import ConversationStore
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
    conversations: ConversationStore | None = None,
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

    It never passes the runner ``capabilities``. With ``conversations``, the store the thread's
    mount reads (scoped as that mount scopes it), a child's thread is started with the
    ``parent`` and ``lineage`` its conversation stores, from which the runner composes its
    bounds; a thread with neither stored, or no ``conversations``, is started with neither.
    When two wakes race, the runner's claim lets one run start; the other's refusal is
    absorbed once the thread reads as running.
    """
    if thread.scope is None:
        raise ValueError(f"waking thread {thread.thread_id!r} needs its scope, the agent id")
    state = await runs.get(thread.thread_id, request=request)
    if state is not None and state.status in _BUSY:
        return None
    if not await queue.pending(thread, request=request, limit=1):
        return None
    stored = (
        None
        if conversations is None
        else await conversations.load(thread.thread_id, request=request)
    )
    try:
        return await runner.start(
            thread.scope,
            prompt,
            request.user,
            timeout=timeout,
            thread_id=thread.thread_id,
            parent=None if stored is None else stored.parent,
            lineage=None if stored is None else stored.lineage,
        )
    except Exception:
        state = await runs.get(thread.thread_id, request=request)
        if state is not None and state.status is ThreadRunStatus.RUNNING:
            return None
        raise


__all__ = ["wake_thread"]
