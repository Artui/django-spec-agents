from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.http import HttpRequest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.child_sessions.child_report import ChildReport
from django_spec_agents.child_sessions.child_sessions_toolset import ChildSessionsToolset
from django_spec_agents.child_sessions.types.child_session import ChildSession
from django_spec_agents.child_sessions.types.child_session_limits import ChildSessionLimits
from django_spec_agents.child_sessions.types.inbound_kind import InboundKind
from django_spec_agents.child_sessions.types.inbound_queue import InboundQueue
from django_spec_agents.child_sessions.waking_inbound_queue import WakingInboundQueue
from django_spec_agents.contrib.store.default_conversation_store import DefaultConversationStore
from django_spec_agents.contrib.store.default_inbound_queue import DefaultInboundQueue
from django_spec_agents.contrib.store.default_run_store import DefaultRunStore
from django_spec_agents.headless.types.headless_start import HeadlessStart
from django_spec_agents.persistence.scoped_conversation_store import ScopedConversationStore
from django_spec_agents.persistence.types.child_lineage_record import ChildLineageRecord
from django_spec_agents.persistence.types.conversation import Conversation
from django_spec_agents.persistence.types.thread_ref import ThreadRef
from django_spec_agents.persistence.types.thread_run_status import ThreadRunStatus
from django_spec_agents.testing import tool_calling_model
from tests.request_helpers import authed_request

pytestmark = pytest.mark.django_db(transaction=True)

_PARENT = ThreadRef("p1", "main")
_CHILD = ThreadRef("c1", "research")
_LINEAGE = ChildLineageRecord(_PARENT, 1, ["_lookup"], [])


class _Runner:
    def __init__(self, runs: DefaultRunStore, fails: bool = False) -> None:
        self.runs, self.fails = runs, fails
        self.starts: list[dict[str, Any]] = []

    async def start(self, agent_id: str, prompt: str, principal: Any, **kwargs: Any) -> Any:
        if self.fails:
            raise LookupError("no headless mount")
        run_id = uuid.uuid4().hex
        request = authed_request(str(principal.pk))
        await self.runs.claim(kwargs["thread_id"], run_id, request=request, lease=timedelta(1))
        self.starts.append({"agent_id": agent_id, **kwargs})
        return HeadlessStart(thread_id=kwargs["thread_id"], run_id=run_id)

    async def run_to_first_stop(self, *args: Any, **kwargs: Any) -> Any: ...

    async def cancel(self, thread_id: str, principal: Any) -> bool: ...


class _Index:
    async def children(self, parent: ThreadRef, *, request: HttpRequest) -> list[ChildSession]:
        return [ChildSession(_CHILD, ThreadRunStatus.IDLE)]

    async def live_count(self, *, request: HttpRequest) -> int: ...


def _waking(runner: _Runner) -> WakingInboundQueue:
    research = ScopedConversationStore(DefaultConversationStore(), scope="research")
    return WakingInboundQueue(
        DefaultInboundQueue(), runner, runner.runs, 60.0, conversations={"research": research}
    )


async def _save_child(request: HttpRequest) -> None:
    store = ScopedConversationStore(DefaultConversationStore(), scope="research")
    child = Conversation("c1", messages=[], parent=_PARENT, lineage=_LINEAGE)
    await store.save(child, request=request)


def test_it_is_an_inbound_queue() -> None:
    assert isinstance(_waking(_Runner(DefaultRunStore())), InboundQueue)


@pytest.mark.parametrize("tool", ["message_task", "answer_task"])
async def test_a_message_to_a_settled_child_wakes_it_within_its_bounds(tool: str) -> None:
    request = authed_request("7")
    await _save_child(request)
    runner = _Runner(DefaultRunStore())
    toolset = ChildSessionsToolset(
        "main",
        agents={"research": "Finds things out."},
        runner=runner,
        index=_Index(),
        queue=_waking(runner),
        limits=ChildSessionLimits(timeout=90.0),
    )
    args = {"thread_id": "c1", "message": "Look again.", "answer": "1999"}
    args = {k: v for k, v in args.items() if k == "thread_id" or tool.startswith(k[:3])}
    agent = Agent(tool_calling_model(tool, args), toolsets=[toolset])

    deps = AgentDeps(user=request.user, call_context={"request": request})
    await agent.run("go", deps=deps, conversation_id="p1")

    [start] = runner.starts
    assert (start["agent_id"], start["thread_id"]) == ("research", "c1")
    assert (start["parent"], start["lineage"]) == (_PARENT, _LINEAGE)
    assert "capabilities" not in start


async def test_a_childs_report_wakes_its_idle_parent_once() -> None:
    request = authed_request("7")
    runner = _Runner(DefaultRunStore())
    queue = _waking(runner)
    child = Agent(FunctionModel(lambda _m, _i: ModelResponse(parts=[TextPart("Found 3.")])))

    for _ in range(2):
        await child.run(
            "Go.",
            deps=AgentDeps(user=request.user, call_context={"request": request}),
            conversation_id="c1",
            capabilities=[ChildReport(_PARENT, "research", queue)],
        )

    [start] = runner.starts
    assert (start["agent_id"], start["thread_id"], start["parent"]) == ("main", "p1", None)
    assert len(await queue.pending(_PARENT, request=request)) == 2


async def test_a_wake_that_fails_is_logged_and_the_message_stays_queued(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = authed_request("7")
    queue = _waking(_Runner(DefaultRunStore(), fails=True))

    with caplog.at_level(logging.ERROR):
        queued = await queue.enqueue(_PARENT, InboundKind.STEER, {"text": "hi"}, request=request)

    [message] = await queue.pending(_PARENT, request=request)
    assert message.id == queued
    assert await queue.ack(_PARENT, [queued], request=request) == 1
    assert "Could not wake" in caplog.text
