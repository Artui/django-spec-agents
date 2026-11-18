from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.http import HttpRequest
from django_service_specs import InvalidArguments, NotPermitted, PermissionCheck, kernel_spec
from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults, Tool
from pydantic_ai.exceptions import CallDeferred
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from django_spec_agents.child_sessions.child_lineage_for import child_lineage_for
from django_spec_agents.contrib.store.default_conversation_store import DefaultConversationStore
from django_spec_agents.contrib.store.default_run_store import DefaultRunStore
from django_spec_agents.headless.run_headless import run_headless
from django_spec_agents.headless.types.firing_queue import FiringQueue
from django_spec_agents.headless.types.firing_state import FiringState
from django_spec_agents.headless.types.headless_firing import HeadlessFiring
from django_spec_agents.headless.types.headless_runner import HeadlessRunner
from django_spec_agents.headless.types.headless_start import HeadlessStart
from django_spec_agents.headless.types.run_stop import RunStop
from django_spec_agents.persistence.build_run_waiting import build_run_waiting
from django_spec_agents.persistence.types.child_lineage_record import ChildLineageRecord
from django_spec_agents.persistence.types.conversation import Conversation
from django_spec_agents.persistence.types.run_end_reason import RunEndReason
from django_spec_agents.persistence.types.run_outcome import RunOutcome
from django_spec_agents.persistence.types.run_waiting import RunWaiting
from django_spec_agents.persistence.types.thread_ref import ThreadRef
from django_spec_agents.persistence.types.thread_run_status import ThreadRunStatus
from tests.headless.task_worker import TaskWorker

pytestmark = pytest.mark.django_db(transaction=True)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_LEASE = timedelta(seconds=30)
_WAITING = RunWaiting(kind="approval", summary="delete_thing", tool_call_id="c1", destructive=True)
_PARKED = RunStop("t1", "r1", RunOutcome.WAITING, _WAITING)
_PARAMS = {"agent_id": "support", "prompt": "Summarise the inbox.", "timeout": 17.5}


class _Clock:
    def __init__(self) -> None:
        self.now = _T0

    def __call__(self) -> datetime:
        return self.now


class _Authenticated(PermissionCheck):
    def has_permission(self, principal: Any, spec: Any) -> bool:
        return principal is not None and principal.is_authenticated


class _Nobody(PermissionCheck):
    def has_permission(self, principal: Any, spec: Any) -> bool:
        return False


class _Runner:
    """A ``HeadlessRunner`` that claims its thread in a real run store, then stops or dies."""

    def __init__(
        self, store: DefaultRunStore, fault: BaseException | None = None, stop: RunStop = _PARKED
    ) -> None:
        self.store, self.fault, self.stop, self.calls = store, fault, stop, []
        self.lineages: list[ChildLineageRecord | None] = []
        self.run_ids: list[str | None] = []
        self.answers: list[Mapping[str, str] | None] = []

    async def start(self, *args: Any, **kwargs: Any) -> HeadlessStart:
        raise NotImplementedError

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
        capabilities: Sequence[Any] = (),
        run_id: str | None = None,
        answers: Mapping[str, str] | None = None,
    ) -> RunStop:
        self.calls.append((agent_id, prompt, principal, timeout, thread_id, parent, capabilities))
        self.lineages.append(lineage)
        self.run_ids.append(run_id)
        self.answers.append(answers)
        request = HttpRequest()
        request.user = principal
        await self.store.claim("t1", "r1", request=request, lease=_LEASE)
        if self.fault is not None:
            raise self.fault
        await self.store.finish(
            "t1", "r1", self.stop.outcome, request=request, waiting=self.stop.waiting
        )
        return self.stop

    async def cancel(self, thread_id: str, principal: Any) -> bool:
        raise NotImplementedError


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def store(clock: _Clock) -> DefaultRunStore:
    return DefaultRunStore(clock=clock)


@pytest.fixture
def user() -> Any:
    return get_user_model().objects.create_user(username="member")


def _worker(runner: HeadlessRunner, clock: _Clock, permissions: Any = None) -> TaskWorker:
    spec = run_headless(runner, permissions=permissions or [_Authenticated()])
    return TaskWorker({"run_headless": spec}, clock, lease=timedelta(minutes=5))


def test_the_doubles_have_the_shapes_they_stand_in_for(store: DefaultRunStore) -> None:
    assert isinstance(_Runner(store), HeadlessRunner)
    assert isinstance(_worker(_Runner(store), _Clock()), FiringQueue)


def test_the_spec_declares_no_transaction_and_no_idempotence(store: DefaultRunStore) -> None:
    spec = kernel_spec(run_headless(_Runner(store), permissions=[_Authenticated()]))

    assert spec.atomic is False
    assert spec.idempotent is False


def test_a_worker_runs_the_spec_to_its_first_stop(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    runner = _Runner(store)
    worker = _worker(runner, clock)
    params = {**_PARAMS, "thread_id": "t1", "parent": {"thread_id": "t0", "scope": "main"}}

    run_id = worker.enqueue("run_headless", params, actor=user)
    assert worker.state(run_id) is FiringState.PENDING
    worker.run(run_id)

    assert worker.rows[run_id].output == {
        "thread_id": "t1",
        "run_id": "r1",
        "outcome": "waiting",
        "waiting": {
            "kind": "approval",
            "summary": "delete_thing",
            "tool_call_id": "c1",
            "destructive": True,
        },
    }
    assert worker.state(run_id) is FiringState.FINISHED
    [(agent_id, prompt, principal, timeout, thread_id, parent, capabilities)] = runner.calls
    assert (agent_id, prompt, timeout, thread_id, parent) == (
        "support",
        "Summarise the inbox.",
        17.5,
        "t1",
        ThreadRef("t0", "main"),
    )
    assert principal is not user and principal.pk == user.pk
    assert capabilities == ()


def test_a_run_that_completes_presents_no_waiting(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    worker = _worker(_Runner(store, stop=RunStop("t1", "r1", RunOutcome.COMPLETED)), clock)
    run_id = worker.enqueue("run_headless", _PARAMS, actor=user)

    worker.run(run_id)

    output = {"thread_id": "t1", "run_id": "r1", "outcome": "completed", "waiting": None}
    assert worker.rows[run_id].output == output
    assert worker.state(run_id) is FiringState.FINISHED


def test_a_principal_deactivated_before_the_worker_lapses_the_firing(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    runner = _Runner(store)
    worker = _worker(runner, clock)
    run_id = worker.enqueue("run_headless", _PARAMS, actor=user)
    user.is_active = False
    user.save()

    worker.run(run_id)

    assert worker.state(run_id) is FiringState.LAPSED
    assert runner.calls == []


def test_the_thread_and_parent_default_to_none(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    runner = _Runner(store)
    worker = _worker(runner, clock)

    worker.run(worker.enqueue("run_headless", _PARAMS, actor=user))

    [(*_, thread_id, parent, _capabilities)] = runner.calls
    assert (thread_id, parent) == (None, None)


def test_a_restart_leaves_the_lease_to_lapse(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    runner = _Runner(store, SystemExit(1))
    worker = _worker(runner, clock)
    run_id = worker.enqueue("run_headless", _PARAMS, actor=user)

    with pytest.raises(SystemExit):
        worker.run(run_id)

    request = HttpRequest()
    request.user = user
    assert async_to_sync(store.get)("t1", request=request).status is ThreadRunStatus.RUNNING
    assert worker.state(run_id) is FiringState.PENDING

    clock.now = _T0 + timedelta(minutes=5, seconds=1)
    lapsed = async_to_sync(store.get)("t1", request=request)
    assert (lapsed.status, lapsed.reason) == (ThreadRunStatus.FAILED, RunEndReason.INTERRUPTED)
    worker.sweep()
    assert worker.state(run_id) is FiringState.LAPSED

    worker.run(run_id)
    assert len(runner.calls) == 1


def test_a_run_whose_spec_is_gone_when_the_sweep_ends_it_has_lapsed(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    worker = _worker(_Runner(store, SystemExit(1)), clock)
    run_id = worker.enqueue("run_headless", _PARAMS, actor=user)
    with pytest.raises(SystemExit):
        worker.run(run_id)

    worker.specs = {}
    clock.now = _T0 + timedelta(minutes=5, seconds=1)
    worker.sweep()

    assert worker.rows[run_id].state == "SPEC_UNAVAILABLE"
    assert worker.state(run_id) is FiringState.LAPSED


def test_a_runner_that_raises_ends_the_firing(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    worker = _worker(_Runner(store, RuntimeError("model unreachable")), clock)
    run_id = worker.enqueue("run_headless", _PARAMS, actor=user)

    worker.run(run_id)

    assert worker.state(run_id) is FiringState.FINISHED


def test_enqueue_refuses_what_the_spec_refuses(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    worker = _worker(_Runner(store), clock)

    with pytest.raises(InvalidArguments):
        worker.enqueue("run_headless", {"agent_id": "support"}, actor=user)
    with pytest.raises(NotPermitted):
        _worker(_Runner(store), clock, [_Nobody()]).enqueue("run_headless", _PARAMS, actor=user)
    assert worker.state("1") is None


def test_a_queued_childs_later_run_stays_inside_its_parents_tools(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    runner = _Runner(store)
    worker = _worker(runner, clock)
    root = {"thread_id": "t0", "scope": "main"}
    lineage = {"root": root, "depth": 1, "tools": ["search"], "gated": []}
    params = {**_PARAMS, "thread_id": "t1", "parent": root, "lineage": lineage}

    worker.run(worker.enqueue("run_headless", params, actor=user))

    [record] = runner.lineages
    assert record == ChildLineageRecord(ThreadRef("t0", "main"), 1, ["search"], [])
    stored = Conversation("t1", parent=ThreadRef("t0", "main"), lineage=record)
    later = child_lineage_for(stored, agent_id="support", owner=str(user.pk), queue=object())
    offered: list[str] = []

    def model(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered.extend(tool.name for tool in info.function_tools)
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(
        FunctionModel(model),
        tools=[Tool(_search, name="search"), Tool(_delete_everything, name="delete_everything")],
    )
    agent.run_sync("Go on.", capabilities=later)

    assert offered == ["search"]


def _search() -> str:
    return "found"  # pragma: no cover


def _delete_everything() -> str:
    return "deleted"  # pragma: no cover


class _RunnerWithoutRunId(_Runner):
    """A runner built against 0.20.0, taking neither ``run_id`` nor ``answers``."""

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
        capabilities: Sequence[Any] = (),
    ) -> RunStop:
        return await super().run_to_first_stop(
            agent_id, prompt, principal, timeout=timeout, thread_id=thread_id, parent=parent
        )


def test_a_firing_carrying_a_claimed_run_id_adopts_it(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    runner = _Runner(store)
    worker = _worker(runner, clock)
    firing = worker.enqueue("run_headless", {**_PARAMS, "run_id": "claimed-7"}, actor=user)

    worker.run(firing)

    assert worker.state(firing) is FiringState.FINISHED
    assert runner.run_ids == ["claimed-7"]


def test_a_firing_without_a_run_id_reaches_a_runner_that_takes_none(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    runner = _RunnerWithoutRunId(store)
    worker = _worker(runner, clock)
    firing = worker.enqueue("run_headless", _PARAMS, actor=user)

    worker.run(firing)

    assert worker.state(firing) is FiringState.FINISHED
    assert runner.run_ids == [None]
    assert runner.answers == [None]


def test_a_firing_s_inbox_mark_reaches_a_runner_that_takes_none(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    runner = _RunnerWithoutRunId(store)
    worker = _worker(runner, clock)
    firing = worker.enqueue("run_headless", {**_PARAMS, "inbox_mark": 41}, actor=user)

    worker.run(firing)

    assert runner.run_ids == [None]


def test_a_firing_carries_its_answers_across_the_queue_as_json(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    runner = _Runner(store)
    worker = _worker(runner, clock)
    params = {**_PARAMS, "thread_id": "t1", "answers": {"q-1": "1999"}}

    worker.run(worker.enqueue("run_headless", params, actor=user))

    assert runner.answers == [{"q-1": "1999"}]


def _ask(question: str) -> str:
    raise CallDeferred({"question": question}, kind="question")


def _asker(seen: list[ModelRequest]) -> FunctionModel:
    def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        assert isinstance(last, ModelRequest)
        seen.append(last)
        returned = [p for p in last.parts if isinstance(p, ToolReturnPart)]
        if returned:
            return ModelResponse(parts=[TextPart(f"It was {returned[0].content}.")])
        return ModelResponse(parts=[ToolCallPart("ask", {"question": "Which year?"}, "q-1")])

    return FunctionModel(respond)


class _AgentRunner:
    """A ``HeadlessRunner`` over a real agent whose history a conversation store keeps."""

    def __init__(self) -> None:
        self.seen: list[ModelRequest] = []
        self.conversations = DefaultConversationStore()

    async def start(self, *args: Any, **kwargs: Any) -> HeadlessStart:
        raise NotImplementedError

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
        capabilities: Sequence[Any] = (),
        run_id: str | None = None,
        answers: Mapping[str, str] | None = None,
    ) -> RunStop:
        assert thread_id is not None
        request = HttpRequest()
        request.user = principal
        stored = await self.conversations.load(thread_id, request=request)
        history = (
            None if stored is None else ModelMessagesTypeAdapter.validate_python(stored.messages)
        )
        resume = None if answers is None else DeferredToolResults(calls=dict(answers))
        agent = Agent(
            _asker(self.seen),
            tools=[Tool(_ask, name="ask")],
            output_type=[str, DeferredToolRequests],
        )
        result = await agent.run(
            None if answers else prompt, message_history=history, deferred_tool_results=resume
        )
        messages = ModelMessagesTypeAdapter.dump_python(result.all_messages(), mode="json")
        await self.conversations.save(
            Conversation(thread_id, messages, owner_id=str(principal.pk)), request=request
        )
        if isinstance(result.output, DeferredToolRequests):
            [call] = result.output.calls
            waiting = build_run_waiting(
                result.output, call.tool_call_id, summary=call.tool_name, destructive=False
            )
            return RunStop(thread_id, "r1", RunOutcome.WAITING, waiting)
        return RunStop(thread_id, "r1", RunOutcome.COMPLETED)

    async def cancel(self, thread_id: str, principal: Any) -> bool:
        raise NotImplementedError


def test_a_firing_with_answers_resumes_the_parked_question_with_no_new_prompt(
    clock: _Clock, user: Any
) -> None:
    runner = _AgentRunner()
    assert isinstance(runner, HeadlessRunner)
    worker = _worker(runner, clock)
    asked = worker.enqueue("run_headless", {**_PARAMS, "thread_id": "c1"}, actor=user)
    worker.run(asked)
    assert worker.rows[asked].output["waiting"]["kind"] == "question"

    params = {**_PARAMS, "thread_id": "c1", "answers": {"q-1": "1999"}}
    answered = worker.enqueue("run_headless", params, actor=user)
    worker.run(answered)

    completed = {"thread_id": "c1", "run_id": "r1", "outcome": "completed", "waiting": None}
    assert worker.rows[answered].output == completed
    resumed = runner.seen[-1]
    [returned] = [p for p in resumed.parts if isinstance(p, ToolReturnPart)]
    assert (returned.tool_call_id, returned.content) == ("q-1", "1999")
    assert not [p for p in resumed.parts if isinstance(p, UserPromptPart)]


def _firings_seen(store: DefaultRunStore, clock: _Clock) -> tuple[TaskWorker, list[HeadlessFiring]]:
    spec = run_headless(_Runner(store), permissions=[_Authenticated()])
    seen: list[HeadlessFiring] = []

    async def spy(user: Any, data: HeadlessFiring) -> RunStop:
        seen.append(data)
        return await spec.service(user, data)

    return TaskWorker({"run_headless": replace(spec, service=spy)}, clock), seen


def test_a_firing_carries_its_inbox_mark_across_the_queue_as_json(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    worker, seen = _firings_seen(store, clock)

    worker.run(worker.enqueue("run_headless", {**_PARAMS, "inbox_mark": 41}, actor=user))

    assert [firing.inbox_mark for firing in seen] == [41]


def test_a_firing_serialised_before_the_mark_existed_still_runs_with_none(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    worker, seen = _firings_seen(store, clock)
    old = json.loads(json.dumps(_PARAMS))

    run_id = worker.enqueue("run_headless", old, actor=user)
    worker.run(run_id)

    assert worker.state(run_id) is FiringState.FINISHED
    assert [firing.inbox_mark for firing in seen] == [None]
    assert HeadlessFiring("support", "go", 5.0).inbox_mark is None
