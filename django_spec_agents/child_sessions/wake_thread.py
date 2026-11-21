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
    once it ends; waiting on questions only, with an ``ANSWER`` pending for each, it resumes
    the parked calls, starting the run with ``answers=`` mapping each waiting call's id, in
    order, to the oldest answers' texts. It acks those messages first, so the resumed run's
    drain does not hand them on again, and starts nothing unless the ack removes them all,
    since another wake took them, enqueueing again any it did remove; a start that fails
    enqueues the answers again. Waiting on
    anything else, or on more questions than there are answers, it does nothing, since a
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
        calls, messages = answer
        acked = [m for m in messages if await queue.ack(thread, [m.id], request=request)]
        if len(acked) < len(messages):
            await _enqueue_again(queue, thread, acked, request)
            return None
        resume["answers"] = {
            call: str(message.body.get("text", ""))
            for call, message in zip(calls, messages, strict=True)
        }
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
        await _enqueue_again(queue, thread, [] if answer is None else answer[1], request)
        state = await runs.get(thread.thread_id, request=request)
        if state is not None and state.status is ThreadRunStatus.RUNNING:
            return None
        raise
    return started


async def _enqueue_again(
    queue: InboundQueue, thread: ThreadRef, messages: list[InboundMessage], request: HttpRequest
) -> None:
    for message in messages:
        await queue.enqueue(
            thread, message.kind, message.body, request=request, sender=message.sender
        )


async def _answer(
    state: ThreadRunState | None, thread: ThreadRef, request: HttpRequest, queue: InboundQueue
) -> tuple[list[str], list[InboundMessage]] | None:
    waiting = () if state is None else state.waiting
    if not waiting or any(call.kind != "question" for call in waiting):
        return None
    pending = await queue.pending(thread, request=request)
    answers = [m for m in pending if m.kind is InboundKind.ANSWER][: len(waiting)]
    if len(answers) < len(waiting):
        return None
    return [call.tool_call_id for call in waiting], answers


__all__ = ["wake_thread"]
