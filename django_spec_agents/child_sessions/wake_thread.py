from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from django_spec_agents.child_sessions.types.inbound_kind import InboundKind
from django_spec_agents.child_sessions.types.inbound_message import InboundMessage
from django_spec_agents.child_sessions.types.inbound_queue import InboundQueue
from django_spec_agents.headless.types.headless_runner import HeadlessRunner
from django_spec_agents.headless.types.headless_start import HeadlessStart
from django_spec_agents.persistence.types.conversation_store import ConversationStore
from django_spec_agents.persistence.types.run_store import RunStore
from django_spec_agents.persistence.types.thread_ref import ThreadRef
from django_spec_agents.persistence.types.thread_run_state import ThreadRunState
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

    Call it after enqueueing for ``thread``, and the runner after each run ends. By the
    thread's state in ``runs``: running, it does nothing, since the run reads the queue at
    its next model request, or, for one that lands after its last read, the runner's wake
    once it ends; waiting on a
    question with an ``ANSWER`` pending, it resumes the parked call, starting the run with
    ``answers=`` mapping the waiting call's id to the oldest answer's text. It acks that
    message first, so the resumed run's drain does not hand it on again, and starts nothing
    when the ack removes nothing, since another wake took it; a start that fails enqueues the
    answer again. Waiting on anything else, it does nothing, since a
    second run must not start before the resume, which reads the queue; idle, failed or never
    run, with messages pending, it starts one run on the thread's scope as ``request.user``,
    with ``prompt`` as its new message (by default a line saying messages arrived), and
    returns it. The run's mount composes
    [`DrainInbound`][django_spec_agents.child_sessions.drain_inbound.DrainInbound] to read them.

    It never passes the runner ``capabilities``. With ``conversations``, the store the thread's
    mount reads (scoped as that mount scopes it), a child's thread is started with the
    ``parent`` and ``lineage`` its conversation stores, from which the runner composes its
    bounds; a thread with neither stored, or no ``conversations``, is started with neither.
    A message another run took and has not released starts nothing: that run hands it on
    when it releases the inbox, and a run that died holding it leaves it for the next run
    the thread starts once its lease lapses. Pass the inner queue, never a
    [`WakingInboundQueue`][django_spec_agents.child_sessions.waking_inbound_queue.WakingInboundQueue]:
    an answer whose start fails is enqueued again, which would wake once more.

    When two wakes race, the runner's claim lets one run start; the other's refusal is
    absorbed once the thread reads as running.
    """
    if thread.scope is None:
        raise ValueError(f"waking thread {thread.thread_id!r} needs its scope, the agent id")
    state = await runs.get(thread.thread_id, request=request)
    answer = await _answer(state, thread, request, queue)
    if answer is None:
        if state is not None and state.status in _BUSY:
            return None
        pending = await queue.pending(thread, request=request)
        if all(message.taken_by is not None for message in pending):
            return None
    stored = (
        None
        if conversations is None
        else await conversations.load(thread.thread_id, request=request)
    )
    resume: dict[str, Any] = {}
    if answer is not None:
        if not await queue.ack(thread, [answer[1].id], request=request):
            return None
        resume["answers"] = {answer[0]: str(answer[1].body.get("text", ""))}
    try:
        started = await runner.start(
            thread.scope,
            prompt,
            request.user,
            timeout=timeout,
            thread_id=thread.thread_id,
            parent=None if stored is None else stored.parent,
            lineage=None if stored is None else stored.lineage,
            **resume,
        )
    except Exception:
        if answer is not None:
            message = answer[1]
            await queue.enqueue(
                thread, message.kind, message.body, request=request, sender=message.sender
            )
        state = await runs.get(thread.thread_id, request=request)
        if state is not None and state.status is ThreadRunStatus.RUNNING:
            return None
        raise
    return started


async def _answer(
    state: ThreadRunState | None, thread: ThreadRef, request: HttpRequest, queue: InboundQueue
) -> tuple[str, InboundMessage] | None:
    waiting = None if state is None else state.waiting
    if waiting is None or waiting.kind != "question":
        return None
    pending = await queue.pending(thread, request=request)
    answer = next((m for m in pending if m.kind is InboundKind.ANSWER), None)
    return None if answer is None else (waiting.tool_call_id, answer)


__all__ = ["wake_thread"]
