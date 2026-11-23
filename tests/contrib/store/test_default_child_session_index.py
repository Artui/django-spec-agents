from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ToolReturnPart

from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.child_sessions.child_sessions_toolset import ChildSessionsToolset
from django_spec_agents.child_sessions.types.child_session import ChildSession
from django_spec_agents.child_sessions.types.child_session_index import ChildSessionIndex
from django_spec_agents.child_sessions.types.child_session_limits import ChildSessionLimits
from django_spec_agents.contrib.store.default_child_session_index import DefaultChildSessionIndex
from django_spec_agents.contrib.store.default_conversation_store import DefaultConversationStore
from django_spec_agents.contrib.store.default_run_store import DefaultRunStore
from django_spec_agents.persistence.anonymous_operation_error import AnonymousOperationError
from django_spec_agents.persistence.scoped_conversation_store import ScopedConversationStore
from django_spec_agents.persistence.types.conversation import Conversation
from django_spec_agents.persistence.types.run_end_reason import RunEndReason
from django_spec_agents.persistence.types.run_outcome import RunOutcome
from django_spec_agents.persistence.types.run_waiting import RunWaiting
from django_spec_agents.persistence.types.thread_ref import ThreadRef
from django_spec_agents.persistence.types.thread_run_status import ThreadRunStatus
from django_spec_agents.testing import tool_calling_model
from tests.request_helpers import anonymous_request, authed_request

pytestmark = pytest.mark.django_db(transaction=True)

_LEASE = timedelta(minutes=5)
_PARENT = ThreadRef("p1", "main")
_WAITING = (
    RunWaiting(kind="approval", summary="delete_thing", tool_call_id="c1", destructive=True),
)
_RUNNING, _IDLE = ThreadRunStatus.RUNNING, ThreadRunStatus.IDLE


class _Runner:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def cancel(self, thread_id: str, principal: Any) -> bool:
        self.cancelled.append(thread_id)
        return True


@pytest.fixture
def runs() -> DefaultRunStore:
    return DefaultRunStore(clock=lambda: datetime.now(UTC))


@pytest.fixture
def index(runs: DefaultRunStore) -> DefaultChildSessionIndex:
    return DefaultChildSessionIndex(runs)


async def _save(
    thread_id: str,
    parent: ThreadRef | None,
    *,
    scope: str | None = None,
    pk: str = "7",
) -> None:
    store = DefaultConversationStore()
    scoped = store if scope is None else ScopedConversationStore(store, scope=scope)
    await scoped.save(
        Conversation(thread_id=thread_id, messages=[], parent=parent), request=authed_request(pk)
    )


async def _run(
    runs: DefaultRunStore, thread_id: str, outcome: RunOutcome | None, pk: str = "7"
) -> None:
    request = authed_request(pk)
    await runs.claim(thread_id, f"run-{thread_id}", request=request, lease=_LEASE)
    if outcome is not None:
        waiting = _WAITING if outcome is RunOutcome.WAITING else ()
        await runs.finish(thread_id, f"run-{thread_id}", outcome, request=request, waiting=waiting)


def test_it_satisfies_the_protocol(index: DefaultChildSessionIndex) -> None:
    assert isinstance(index, ChildSessionIndex)


async def test_it_lists_a_parents_children_across_scopes_oldest_first(
    index: DefaultChildSessionIndex, runs: DefaultRunStore
) -> None:
    await _save("c1", _PARENT, scope="research")
    await _save("c2", _PARENT, scope="writer")
    await _save("c3", _PARENT)
    await _run(runs, "c1", None)
    await _run(runs, "c2", RunOutcome.WAITING)

    children = await index.children(_PARENT, request=authed_request())

    assert children == [
        ChildSession(ThreadRef("c1", "research"), _RUNNING),
        ChildSession(ThreadRef("c2", "writer"), ThreadRunStatus.WAITING, _WAITING),
        ChildSession(ThreadRef("c3"), _IDLE),
    ]


async def test_an_unlinked_child_is_no_longer_listed(index: DefaultChildSessionIndex) -> None:
    await _save("c1", _PARENT, scope="research")
    await _save("c2", _PARENT)
    scoped = ScopedConversationStore(DefaultConversationStore(), scope="research")

    await scoped.unlink_parent("c1", request=authed_request())

    assert await index.children(_PARENT, request=authed_request()) == [
        ChildSession(ThreadRef("c2"), _IDLE)
    ]


async def test_a_child_whose_run_ended_early_carries_why(
    index: DefaultChildSessionIndex, runs: DefaultRunStore
) -> None:
    await _save("c1", _PARENT, scope="research")
    await _run(runs, "c1", RunOutcome.TIMED_OUT)

    [child] = await index.children(_PARENT, request=authed_request())

    assert (child.status, child.reason) == (_IDLE, RunEndReason.TIMED_OUT)


async def test_it_skips_other_parents_and_the_same_thread_id_in_another_scope(
    index: DefaultChildSessionIndex,
) -> None:
    await _save("c1", _PARENT, scope="research")
    await _save("c2", ThreadRef("p1", "other"), scope="research")
    await _save("c3", ThreadRef("p2", "main"), scope="research")
    await _save("c4", None, scope="research")

    children = await index.children(_PARENT, request=authed_request())

    assert [c.thread for c in children] == [ThreadRef("c1", "research")]


async def test_it_does_not_list_another_owners_children(
    index: DefaultChildSessionIndex, runs: DefaultRunStore
) -> None:
    await _save("c1", _PARENT, scope="research", pk="8")
    await _run(runs, "c1", None, pk="8")

    assert await index.children(_PARENT, request=authed_request("7")) == []
    assert await index.live_count(request=authed_request("7")) == 0
    assert await index.live_count(request=authed_request("8")) == 1


async def test_it_refuses_an_anonymous_request(index: DefaultChildSessionIndex) -> None:
    with pytest.raises(AnonymousOperationError):
        await index.children(_PARENT, request=anonymous_request())
    with pytest.raises(AnonymousOperationError):
        await index.live_count(request=anonymous_request())


async def test_the_count_matches_the_live_children_under_any_parent(
    index: DefaultChildSessionIndex, runs: DefaultRunStore
) -> None:
    for thread_id, parent, scope in [
        ("c1", _PARENT, "research"),
        ("c2", ThreadRef("p9", "main"), "writer"),
        ("c3", _PARENT, "research"),
        ("c4", _PARENT, None),
        ("c5", _PARENT, "research"),
    ]:
        await _save(thread_id, parent, scope=scope)
    await _save("root", None)
    await _run(runs, "c1", None)
    await _run(runs, "c2", RunOutcome.WAITING)
    await _run(runs, "c3", RunOutcome.COMPLETED)
    await _run(runs, "c5", RunOutcome.ERROR)
    await _run(runs, "root", None)

    assert await index.live_count(request=authed_request()) == 2


async def test_the_toolset_lists_and_cancels_against_it(
    index: DefaultChildSessionIndex, runs: DefaultRunStore
) -> None:
    await _save("c1", _PARENT, scope="research")
    await _save("g1", ThreadRef("c1", "research"), scope="writer")
    await _run(runs, "c1", None)
    await _run(runs, "g1", None)
    runner = _Runner()
    toolset = ChildSessionsToolset(
        "main",
        agents={"research": "Finds things out."},
        runner=runner,  # ty: ignore[invalid-argument-type]
        index=index,
        queue=None,  # ty: ignore[invalid-argument-type]
        limits=ChildSessionLimits(timeout=90.0),
    )

    async def call(tool: str, args: dict[str, Any] | None = None) -> Any:
        agent = Agent(tool_calling_model(tool, args), toolsets=[toolset])
        user = authed_request().user  # ty: ignore[unresolved-attribute]
        deps = AgentDeps(user=user, call_context={"request": authed_request()})
        result = await agent.run("go", deps=deps, conversation_id="p1")
        return next(
            part.content
            for message in result.all_messages()
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        )

    listed = await call("list_tasks")
    cancelled = await call("cancel_task", {"thread_id": "c1"})

    assert listed == [ChildSession(ThreadRef("c1", "research"), _RUNNING)]
    assert cancelled == "cancelled 2 running session(s)"
    assert runner.cancelled == ["g1", "c1"]
