from __future__ import annotations

from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

from django.http import HttpRequest

from django_spec_agents.child_sessions.types.inbound_kind import InboundKind
from django_spec_agents.child_sessions.types.inbound_queue import InboundQueue
from django_spec_agents.child_sessions.types.message_origin import MessageOrigin
from django_spec_agents.child_sessions.types.message_origin_key import MESSAGE_ORIGIN_KEY
from django_spec_agents.headless.types.headless_runner import HeadlessRunner
from django_spec_agents.headless.types.headless_start import HeadlessStart
from django_spec_agents.headless.types.prompt_metadata_runner import PromptMetadataRunner
from django_spec_agents.persistence.types.conversation_store import ConversationStore
from django_spec_agents.persistence.types.run_store import RunStore
from django_spec_agents.persistence.types.run_waiting import RunWaiting
from django_spec_agents.persistence.types.thread_ref import ThreadRef
from django_spec_agents.persistence.types.thread_run_state import ThreadRunState
from django_spec_agents.persistence.types.thread_run_status import ThreadRunStatus

_WAKE_PROMPT = "New messages for this conversation arrived while it was idle."
_BUSY = (ThreadRunStatus.RUNNING, ThreadRunStatus.WAITING)
_PAIRING_LEASE = timedelta(seconds=30)


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
    order, to the oldest answers' texts. It pairs them under a claim on the inbox (``take``,
    then ``release``) held across the start, so of two wakes racing on one park only one
    pairs them, and the resumed run's drain takes nothing until the wake acks them once the
    start returns; a wake that finds the inbox claimed starts nothing, and a start that
    fails, is cancelled or dies before the run claims the thread leaves the answers pending
    for the next wake. An ack lost once the run started lets its drain deliver them again,
    as the queue's at-least-once, and the claim holds the thread's other messages from the
    resumed run until the wake releases it. Waiting on
    anything else, or on more questions than there are answers, it does nothing, since a
    second run must not start before the resume, which reads the queue; idle, failed or never
    run, with messages pending, it starts one run on the thread's scope as ``request.user``,
    with ``prompt`` as its new message (by default a line saying messages arrived), and
    returns it. For a runner opted in as a
    [`PromptMetadataRunner`][django_spec_agents.PromptMetadataRunner], that start also passes
    ``prompt_metadata={"origin": "wake"}``, for the runner to keep on the stored message, whatever
    ``prompt`` says; a resume sends no prompt and passes none. The run's mount composes
    [`DrainInbound`][django_spec_agents.child_sessions.drain_inbound.DrainInbound] to read them.

    It never passes the runner ``capabilities``. With ``conversations``, the store the thread's
    mount reads (scoped as that mount scopes it), a child's thread is started with the
    ``parent`` and ``lineage`` its conversation stores, from which the runner composes its
    bounds; a thread with neither stored, or no ``conversations``, is started with neither.
    It passes the stored ``routine_id`` the same way, and as ``parent_run_id`` the parked
    run a resume answers, ``None`` for any other start.
    A message another run took and has not released starts nothing: that run hands it on
    when it releases the inbox, and a run that died holding it leaves it for the next run
    the thread starts once its lease lapses.

    When two wakes race, the runner's claim lets one run start; the other's refusal is
    absorbed once the thread reads as running.
    """
    if thread.scope is None:
        raise ValueError(f"waking thread {thread.thread_id!r} needs its scope, the agent id")
    state = await runs.get(thread.thread_id, request=request)
    waiting = _questions(state)
    if waiting is None:
        if state is not None and state.status in _BUSY:
            return None
        pending = await queue.pending(thread, request=request)
        if all(message.taken_by is not None for message in pending):
            return None
        extra: dict[str, Any] = {"parent_run_id": None}
        # The value test is held by test_a_runner_that_has_not_opted_in_is_woken_unmarked.
        if isinstance(runner, PromptMetadataRunner) and runner.accepts_prompt_metadata:
            extra["prompt_metadata"] = {MESSAGE_ORIGIN_KEY: MessageOrigin.WAKE.value}
        return await _start(
            thread.scope, thread, request, runner, runs, timeout, conversations, prompt, extra
        )
    holder = f"wake-{uuid4().hex}"
    taken = await queue.take(thread, holder, request=request, lease=_PAIRING_LEASE)
    try:
        answers = [m for m in taken if m.kind is InboundKind.ANSWER][: len(waiting)]
        if len(answers) < len(waiting):
            return None
        texts = {
            call.tool_call_id: str(message.body.get("text", ""))
            for call, message in zip(waiting, answers, strict=True)
        }
        started = await _start(
            thread.scope,
            thread,
            request,
            runner,
            runs,
            timeout,
            conversations,
            prompt,
            {
                "answers": texts,
                "parent_run_id": cast("ThreadRunState", state).last_run_id,
            },
        )
        if started is not None:
            await queue.ack(thread, [m.id for m in answers], request=request)
        return started
    finally:
        await queue.release(thread, holder, request=request)


async def _start(
    agent_id: str,
    thread: ThreadRef,
    request: HttpRequest,
    runner: HeadlessRunner,
    runs: RunStore,
    timeout: float,
    conversations: ConversationStore | None,
    prompt: str,
    extra: dict[str, Any],
) -> HeadlessStart | None:
    stored = (
        None
        if conversations is None
        else await conversations.load(thread.thread_id, request=request)
    )
    try:
        return await runner.start(
            agent_id,
            prompt,
            request.user,
            timeout=timeout,
            thread_id=thread.thread_id,
            parent=None if stored is None else stored.parent,
            lineage=None if stored is None else stored.lineage,
            routine_id=None if stored is None else stored.routine_id,
            **extra,
        )
    except Exception:
        state = await runs.get(thread.thread_id, request=request)
        if state is not None and state.status is ThreadRunStatus.RUNNING:
            return None
        raise


def _questions(state: ThreadRunState | None) -> tuple[RunWaiting, ...] | None:
    waiting = () if state is None else state.waiting
    if not waiting or any(call.kind != "question" for call in waiting):
        return None
    return waiting


__all__ = ["wake_thread"]
