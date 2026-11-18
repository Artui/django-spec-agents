from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

import pytest
from django.http import HttpRequest
from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.child_sessions.drain_inbound import DrainInbound
from django_spec_agents.child_sessions.types.inbound_kind import InboundKind
from django_spec_agents.child_sessions.wake_thread import wake_thread
from django_spec_agents.contrib.store.default_conversation_store import DefaultConversationStore
from django_spec_agents.contrib.store.default_inbound_queue import DefaultInboundQueue
from django_spec_agents.contrib.store.default_run_store import DefaultRunStore
from django_spec_agents.headless.types.headless_start import HeadlessStart
from django_spec_agents.persistence.scoped_conversation_store import ScopedConversationStore
from django_spec_agents.persistence.types.child_lineage_record import ChildLineageRecord
from django_spec_agents.persistence.types.conversation import Conversation
from django_spec_agents.persistence.types.run_outcome import RunOutcome
from django_spec_agents.persistence.types.run_waiting import RunWaiting
from django_spec_agents.persistence.types.thread_ref import ThreadRef
from tests.request_helpers import authed_request

_PARENT = ThreadRef("p1", "main")
_CHILD = ThreadRef("c1", "research")
_LEASE = timedelta(minutes=5)


class _Runner:
    def __init__(self, runs: DefaultRunStore, fails: Exception | None = None) -> None:
        self.runs, self.fails = runs, fails
        self.starts: list[dict[str, Any]] = []
        self.attempts = 0

    async def start(
        self,
        agent_id: str,
        prompt: str,
        principal: Any,
        *,
        timeout: float,
        thread_id: str | None = None,
        parent: ThreadRef | None = None,
        lineage: Any = None,
        capabilities: Sequence[Any] = (),
        answers: Mapping[str, str] | None = None,
    ) -> HeadlessStart:
        self.attempts += 1
        if self.fails is not None:
            raise self.fails
        assert thread_id is not None
        run_id = uuid.uuid4().hex
        request = authed_request(str(principal.pk))
        claim = await self.runs.claim(thread_id, run_id, request=request, lease=_LEASE)
        if not claim.claimed:
            raise RuntimeError(f"thread {thread_id} is busy")
        self.starts.append(
            {
                "agent_id": agent_id,
                "prompt": prompt,
                "principal": principal,
                "timeout": timeout,
                "thread_id": thread_id,
                "parent": parent,
                "lineage": lineage,
                "capabilities": capabilities,
                "answers": answers,
            }
        )
        return HeadlessStart(thread_id=thread_id, run_id=run_id)


class _RunnerWithoutAnswers(_Runner):
    async def start(
        self,
        agent_id: str,
        prompt: str,
        principal: Any,
        *,
        timeout: float,
        thread_id: str | None = None,
        parent: ThreadRef | None = None,
        lineage: Any = None,
        capabilities: Sequence[Any] = (),
    ) -> HeadlessStart:
        return await super().start(
            agent_id, prompt, principal, timeout=timeout, thread_id=thread_id
        )


async def _report(request: HttpRequest, text: str = "Found 3.") -> None:
    await DefaultInboundQueue().enqueue(
        _PARENT, InboundKind.REPORT, {"text": text, "outcome": "ok"}, request=request, sender=_CHILD
    )


async def _wake(
    runner: _Runner, request: HttpRequest, thread: ThreadRef = _PARENT, **kwargs: Any
) -> HeadlessStart | None:
    return await wake_thread(
        thread,
        request=request,
        runner=runner,
        runs=runner.runs,
        queue=DefaultInboundQueue(),
        timeout=60.0,
        **kwargs,
    )


async def _send(
    request: HttpRequest, kind: InboundKind, text: str, thread: ThreadRef = _CHILD
) -> int:
    return await DefaultInboundQueue().enqueue(
        thread, kind, {"text": text}, request=request, sender=_PARENT
    )


async def _park(runs: DefaultRunStore, request: HttpRequest, kind: Any, thread: str) -> None:
    await runs.claim(thread, "parked", request=request, lease=_LEASE)
    waiting = RunWaiting(kind=kind, summary="delete_thing", tool_call_id="t-1", destructive=False)
    await runs.finish(thread, "parked", RunOutcome.WAITING, request=request, waiting=waiting)


@pytest.mark.django_db(transaction=True)
async def test_an_idle_thread_with_messages_starts_one_run_with_no_bounds_passed() -> None:
    request = authed_request("7")
    runner = _Runner(DefaultRunStore())
    await _report(request)

    started = await _wake(runner, request)

    assert started is not None and started.thread_id == "p1"
    [start] = runner.starts
    assert (start["agent_id"], start["thread_id"], start["timeout"]) == ("main", "p1", 60.0)
    assert start["principal"] is request.user
    assert (start["parent"], start["lineage"], start["capabilities"]) == (None, None, ())
    assert "arrived" in start["prompt"]


@pytest.mark.django_db(transaction=True)
async def test_a_thread_that_never_ran_or_failed_is_woken() -> None:
    request = authed_request("7")
    runs = DefaultRunStore()
    await runs.claim("p1", "old", request=request, lease=_LEASE)
    await runs.finish("p1", "old", RunOutcome.ERROR, request=request)
    await _report(request)
    runner = _Runner(runs)

    assert await _wake(runner, request) is not None
    assert len(runner.starts) == 1


@pytest.mark.django_db(transaction=True)
async def test_a_second_wake_while_the_run_holds_the_thread_starts_nothing() -> None:
    request = authed_request("7")
    runner = _Runner(DefaultRunStore())
    await _report(request, "one")
    first = await _wake(runner, request)
    await _report(request, "two")

    second = await _wake(runner, request)

    assert first is not None and second is None
    assert (len(runner.starts), runner.attempts) == (1, 1)


@pytest.mark.django_db(transaction=True)
async def test_two_wakes_that_race_start_one_run() -> None:
    request = authed_request("7")
    runner = _Runner(DefaultRunStore())
    await _report(request)

    results = await asyncio.gather(_wake(runner, request), _wake(runner, request))

    assert sorted(result is None for result in results) == [False, True]
    assert len(runner.starts) == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["approval", "question"])
async def test_a_waiting_thread_is_held_until_its_resume(kind: str) -> None:
    request = authed_request("7")
    runs = DefaultRunStore()
    await _park(runs, request, kind, "p1")
    await _report(request)
    runner = _Runner(runs)

    assert await _wake(runner, request) is None
    assert runner.starts == []
    assert len(await DefaultInboundQueue().pending(_PARENT, request=request)) == 1


@pytest.mark.django_db(transaction=True)
async def test_a_child_waiting_on_its_question_resumes_with_the_answer_and_acks_only_it() -> None:
    request = authed_request("7")
    runs = DefaultRunStore()
    await _park(runs, request, "question", "c1")
    steer = await _send(request, InboundKind.STEER, "Use March.")
    await _send(request, InboundKind.ANSWER, "1999")
    later = await _send(request, InboundKind.ANSWER, "2000")
    runner = _Runner(runs)

    started = await _wake(runner, request, _CHILD)

    assert started is not None
    [start] = runner.starts
    assert (start["agent_id"], start["thread_id"]) == ("research", "c1")
    assert start["answers"] == {"t-1": "1999"}
    left = await DefaultInboundQueue().pending(_CHILD, request=request)
    assert [(m.id, m.body["text"]) for m in left] == [(steer, "Use March."), (later, "2000")]


class _RunnerSeeingTheQueue(_Runner):
    async def start(self, agent_id: str, prompt: str, principal: Any, **kwargs: Any) -> Any:
        request = authed_request(str(principal.pk))
        self.queued = await DefaultInboundQueue().pending(_CHILD, request=request)
        return await super().start(agent_id, prompt, principal, **kwargs)


@pytest.mark.django_db(transaction=True)
async def test_the_resumed_run_finds_its_answer_gone_from_the_queue() -> None:
    request = authed_request("7")
    runs = DefaultRunStore()
    await _park(runs, request, "question", "c1")
    await _send(request, InboundKind.ANSWER, "1999")
    runner = _RunnerSeeingTheQueue(runs)

    assert await _wake(runner, request, _CHILD) is not None
    assert runner.queued == []


class _QueueAnotherWakeEmpties(DefaultInboundQueue):
    async def pending(self, thread: ThreadRef, **kwargs: Any) -> Any:
        found = await super().pending(thread, **kwargs)
        await super().ack(thread, [m.id for m in found], request=kwargs["request"])
        return found


@pytest.mark.django_db(transaction=True)
async def test_an_answer_another_wake_took_starts_nothing() -> None:
    request = authed_request("7")
    runs = DefaultRunStore()
    await _park(runs, request, "question", "c1")
    await _send(request, InboundKind.ANSWER, "1999")
    runner = _Runner(runs)

    started = await wake_thread(
        _CHILD,
        request=request,
        runner=runner,
        runs=runs,
        queue=_QueueAnotherWakeEmpties(),
        timeout=60.0,
    )

    assert started is None
    assert runner.attempts == 0


@pytest.mark.django_db(transaction=True)
async def test_a_child_waiting_on_an_approval_is_held_with_an_answer_pending() -> None:
    request = authed_request("7")
    runs = DefaultRunStore()
    await _park(runs, request, "approval", "c1")
    await _send(request, InboundKind.ANSWER, "1999")
    runner = _Runner(runs)

    assert await _wake(runner, request, _CHILD) is None
    assert runner.starts == []


@pytest.mark.django_db(transaction=True)
async def test_an_answer_whose_resume_fails_to_start_stays_pending() -> None:
    request = authed_request("7")
    runs = DefaultRunStore()
    await _park(runs, request, "question", "c1")
    await _send(request, InboundKind.ANSWER, "1999")
    runner = _Runner(runs, fails=LookupError("no headless mount has the id 'research'"))

    with pytest.raises(LookupError):
        await _wake(runner, request, _CHILD)

    [kept] = await DefaultInboundQueue().pending(_CHILD, request=request)
    assert (kept.kind, kept.body, kept.sender) == (InboundKind.ANSWER, {"text": "1999"}, _PARENT)


@pytest.mark.django_db(transaction=True)
async def test_an_idle_thread_wakes_a_runner_that_takes_no_answers() -> None:
    request = authed_request("7")
    await _report(request)
    runner = _RunnerWithoutAnswers(DefaultRunStore())

    assert await _wake(runner, request) is not None
    [start] = runner.starts
    assert start["answers"] is None


@pytest.mark.django_db(transaction=True)
async def test_an_idle_thread_with_nothing_pending_is_not_woken() -> None:
    request = authed_request("7")
    runner = _Runner(DefaultRunStore())

    assert await _wake(runner, request) is None
    assert runner.starts == []


async def test_a_thread_without_its_scope_is_refused() -> None:
    runner = _Runner(DefaultRunStore())

    with pytest.raises(ValueError, match="needs its scope"):
        await _wake(runner, authed_request("7"), ThreadRef("p1"))


@pytest.mark.django_db(transaction=True)
async def test_a_start_that_fails_on_an_idle_thread_raises() -> None:
    request = authed_request("7")
    await _report(request)
    runner = _Runner(DefaultRunStore(), fails=LookupError("no headless mount has the id 'main'"))

    with pytest.raises(LookupError, match="no headless mount"):
        await _wake(runner, request)


def _parent_model(seen: list[list[str]], script: Any) -> FunctionModel:
    def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        seen.append([str(m) for m in messages])
        return script(len(seen))

    return FunctionModel(respond)


async def _parent_run(
    model: FunctionModel, request: HttpRequest, prompt: Any, **kwargs: Any
) -> Any:
    agent = Agent(model, output_type=[str, DeferredToolRequests])

    @agent.tool_plain(requires_approval=True)
    def delete_thing() -> str:
        return "deleted"

    return await agent.run(
        prompt,
        deps=AgentDeps(user=None, call_context={"request": request}),
        conversation_id=_PARENT.thread_id,
        capabilities=[DrainInbound(DefaultInboundQueue(), "main")],
        **kwargs,
    )


@pytest.mark.django_db(transaction=True)
async def test_an_idle_parent_wakes_once_and_reads_every_report_in_that_run() -> None:
    request = authed_request("7")
    runs = DefaultRunStore()
    runner = _Runner(runs)
    await _report(request, "alpha")
    started = await _wake(runner, request)
    await _report(request, "beta")
    assert await _wake(runner, request) is None
    assert started is not None
    seen: list[list[str]] = []

    await _parent_run(
        _parent_model(seen, lambda _step: ModelResponse(parts=[TextPart("ok")])),
        request,
        runner.starts[0]["prompt"],
    )
    await runs.finish("p1", started.run_id, RunOutcome.COMPLETED, request=request)

    assert len(seen) == 1 and "alpha" in str(seen[0]) and "beta" in str(seen[0])
    assert await _wake(runner, request) is None
    assert len(runner.starts) == 1


@pytest.mark.django_db(transaction=True)
async def test_a_waiting_parent_gets_the_report_after_its_resume_and_no_second_run() -> None:
    request = authed_request("7")
    runs = DefaultRunStore()
    runner = _Runner(runs)
    gated = ModelResponse(parts=[ToolCallPart("delete_thing", {}, tool_call_id="gate-1")])
    parked = await _parent_run(_parent_model([], lambda _step: gated), request, "Delete it.")
    await _park(runs, request, "approval", "p1")
    await _report(request, "while you waited")

    assert await _wake(runner, request) is None
    seen: list[list[str]] = []
    await _parent_run(
        _parent_model(seen, lambda _step: ModelResponse(parts=[TextPart("done")])),
        request,
        None,
        message_history=parked.all_messages(),
        deferred_tool_results=DeferredToolResults(approvals={"gate-1": True}),
    )

    assert runner.starts == []
    assert "while you waited" in str(seen[0])
    assert await DefaultInboundQueue().pending(_PARENT, request=request) == []


@pytest.mark.django_db(transaction=True)
async def test_a_steered_idle_child_wakes_with_its_stored_parent_and_lineage() -> None:
    request = authed_request("7")
    lineage = ChildLineageRecord(_PARENT, 1, ["search"], [])
    store = ScopedConversationStore(DefaultConversationStore(), scope="research")
    await store.save(
        Conversation("c1", owner_id="7", parent=_PARENT, lineage=lineage), request=request
    )
    await DefaultInboundQueue().enqueue(
        _CHILD, InboundKind.STEER, {"text": "Use March."}, request=request, sender=_PARENT
    )
    runner = _Runner(DefaultRunStore())

    started = await _wake(runner, request, _CHILD, conversations=store)

    assert started is not None
    [start] = runner.starts
    assert (start["agent_id"], start["thread_id"]) == ("research", "c1")
    assert (start["parent"], start["lineage"], start["capabilities"]) == (_PARENT, lineage, ())


@pytest.mark.django_db(transaction=True)
async def test_a_thread_with_no_stored_conversation_wakes_with_no_lineage() -> None:
    request = authed_request("7")
    await _report(request)
    runner = _Runner(DefaultRunStore())

    await _wake(runner, request, conversations=DefaultConversationStore())

    [start] = runner.starts
    assert (start["parent"], start["lineage"]) == (None, None)
