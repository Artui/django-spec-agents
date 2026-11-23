from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

import pytest
from django.http import HttpRequest
from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai_harness.spend_limits import InMemorySpendStore, SpendLimits, Spent
from pydantic_ai_harness.sub_agents import SubAgent, SubAgents, TaskControl

from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.child_sessions.child_lineage import ChildLineage
from django_spec_agents.child_sessions.child_lineage_for import child_lineage_for
from django_spec_agents.child_sessions.child_sessions_toolset import ChildSessionsToolset
from django_spec_agents.child_sessions.types.child_session import ChildSession
from django_spec_agents.child_sessions.types.child_session_limits import ChildSessionLimits
from django_spec_agents.child_sessions.types.child_session_receipt import ChildSessionReceipt
from django_spec_agents.child_sessions.types.child_session_refusal import ChildSessionRefusal
from django_spec_agents.child_sessions.types.child_session_spend import ChildSessionSpend
from django_spec_agents.child_sessions.types.inbound_kind import InboundKind
from django_spec_agents.headless.types.headless_start import HeadlessStart
from django_spec_agents.persistence.build_run_waiting import build_run_waiting
from django_spec_agents.persistence.types.child_lineage_record import ChildLineageRecord
from django_spec_agents.persistence.types.conversation import Conversation
from django_spec_agents.persistence.types.run_end_reason import RunEndReason
from django_spec_agents.persistence.types.run_waiting import RunWaiting
from django_spec_agents.persistence.types.thread_ref import ThreadRef
from django_spec_agents.persistence.types.thread_run_status import ThreadRunStatus
from django_spec_agents.testing import tool_calling_model
from django_spec_agents.types.self_gated_metadata_key import SELF_GATED_METADATA_KEY

_HERE = ThreadRef("p1", "main")
_RUNNING, _WAITING, _IDLE = ThreadRunStatus.RUNNING, ThreadRunStatus.WAITING, ThreadRunStatus.IDLE


@dataclass
class _User:
    pk: int = 7
    is_authenticated: bool = True


@dataclass
class _Index:
    tree: dict[ThreadRef, list[ChildSession]] = field(default_factory=dict)
    live: int = 0

    async def children(self, parent: ThreadRef, *, request: HttpRequest) -> list[ChildSession]:
        read = list(self.tree.get(parent, []))
        await asyncio.sleep(0)
        return read

    async def live_count(self, *, request: HttpRequest) -> int:
        return self.live


@dataclass
class _Queue:
    sent: list[tuple[ThreadRef, InboundKind, Any, ThreadRef | None]] = field(default_factory=list)

    async def enqueue(
        self,
        thread: ThreadRef,
        kind: InboundKind,
        body: Any,
        *,
        request: HttpRequest,
        sender: ThreadRef | None = None,
    ) -> int:
        self.sent.append((thread, kind, body, sender))
        return len(self.sent)


@dataclass
class _Runner:
    starts: list[dict[str, Any]] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    index: _Index | None = None

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
        parent_run_id: str | None = None,
        routine_id: str | None = None,
    ) -> HeadlessStart:
        self.starts.append(
            dict(
                agent_id=agent_id,
                principal=principal,
                timeout=timeout,
                parent=parent,
                lineage=lineage,
                parent_run_id=parent_run_id,
                routine_id=routine_id,
            )
        )
        self.starts[-1]["capabilities"] = list(capabilities)
        if self.index is not None:
            self.index.tree.setdefault(_HERE, []).append(_child(f"c{len(self.starts)}"))
        return HeadlessStart(thread_id="c-new", run_id="r-new")

    async def run_to_first_stop(self, *args: Any, **kwargs: Any) -> Any: ...

    async def cancel(self, thread_id: str, principal: Any) -> bool:
        self.cancelled.append(thread_id)
        return True


class _SpentStore(InMemorySpendStore):
    def __init__(self, spent_scope: str) -> None:
        super().__init__()
        self.spent_scope = spent_scope

    async def get_many(self, keys: Sequence[str]) -> dict[str, Spent]:
        return {k: Spent(usd=Decimal(9) * (f"|{self.spent_scope}|" in k)) for k in keys}


@dataclass
class _GateLookup(AbstractCapability[Any]):
    async def prepare_tools(
        self, ctx: Any, tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        return [replace(d, kind="unapproved") if d.name == "_lookup" else d for d in tool_defs]


def _child(thread_id: str, status: ThreadRunStatus = _RUNNING) -> ChildSession:
    return ChildSession(ThreadRef(thread_id, "research"), status)


def _toolset(
    index: _Index | None = None,
    runner: _Runner | None = None,
    queue: _Queue | None = None,
    spend: ChildSessionSpend | None = None,
) -> ChildSessionsToolset:
    return ChildSessionsToolset(
        "main",
        agents={"research": "Finds things out."},
        runner=runner or _Runner(),
        index=index or _Index(),
        queue=queue or _Queue(),
        limits=ChildSessionLimits(timeout=90.0),
        spend=spend,
    )


def _lookup() -> str:
    return "found"


def _composed(start: dict[str, Any], spend: ChildSessionSpend | None = None) -> list[Any]:
    stored = Conversation("c-new", parent=start["parent"], lineage=start["lineage"])
    owner = str(start["principal"].pk)
    return child_lineage_for(stored, agent_id="research", owner=owner, queue=_Queue(), spend=spend)


async def _call(
    toolset: Any,
    tool: str,
    args: dict[str, Any] | None = None,
    *,
    lineage: ChildLineage | None = None,
    user: Any = None,
) -> Any:
    agent = Agent(
        tool_calling_model(tool, args),
        toolsets=[toolset, FunctionToolset([_lookup])],
        output_type=[str, DeferredToolRequests],
    )
    deps = AgentDeps(user=user or _User(), call_context={"request": HttpRequest()})
    capabilities = [lineage] if lineage else []
    result = await agent.run("go", deps=deps, conversation_id="p1", capabilities=capabilities)
    returns = [
        part.content
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    return returns[0] if returns else result.output


def _lineage(depth: int, tools: frozenset[str] = frozenset({"_lookup"})) -> ChildLineage:
    return ChildLineage(
        root=ThreadRef("root1", "main"), parent=ThreadRef("p0"), depth=depth, tools=tools
    )


async def test_the_receipt_names_the_child_thread() -> None:
    runner = _Runner()

    receipt = await _call(
        _toolset(runner=runner), "start_session", {"agent": "research", "prompt": "Look."}
    )

    assert receipt == ChildSessionReceipt("research", "c-new", "r-new")
    [start] = runner.starts
    assert (start["agent_id"], start["parent"], start["timeout"]) == ("research", _HERE, 90.0)
    assert start["principal"] == _User()
    assert start["capabilities"] == []
    [lineage, report] = _composed(start)
    assert (lineage.root, lineage.parent, lineage.depth) == (_HERE, _HERE, 1)
    assert (report.parent, report.agent_id) == (_HERE, "research")
    assert start["lineage"] == ChildLineageRecord(
        _HERE, 1, sorted(lineage.tools), sorted(lineage.gated)
    )


async def test_the_child_names_the_run_that_started_it() -> None:
    runner = _Runner()

    def model(messages: Any, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(
                [ToolCallPart("start_session", {"agent": "research", "prompt": "x"})]
            )
        return ModelResponse([TextPart("done")])

    agent = Agent(FunctionModel(model), toolsets=[_toolset(runner=runner)])
    deps = AgentDeps(user=_User(), call_context={"request": HttpRequest()})
    result = await agent.run("go", deps=deps, conversation_id="p1")

    [start] = runner.starts
    assert result.run_id is not None
    assert (start["parent_run_id"], start["routine_id"]) == (result.run_id, None)


async def test_the_child_gets_the_parents_tools_and_no_others() -> None:
    runner = _Runner()

    await _call(_toolset(runner=runner), "start_session", {"agent": "research", "prompt": "Look."})

    tools = _composed(runner.starts[0])[0].tools
    assert "_lookup" in tools and "start_session" in tools
    assert "delete_everything" not in tools


async def test_a_child_cannot_call_a_tool_its_parent_lacks() -> None:
    offered: list[set[str]] = []

    def model(messages: Any, info: AgentInfo) -> ModelResponse:
        offered.append({tool.name for tool in info.function_tools})
        return ModelResponse(parts=[ToolCallPart("_wider", {})])

    def _wider() -> str:
        return "ran"

    agent = Agent(FunctionModel(model), toolsets=[FunctionToolset([_lookup, _wider])])

    with pytest.raises(Exception, match="_wider"):
        await agent.run("go", deps=AgentDeps(user=None), capabilities=[_lineage(1)])
    assert offered[0] == {"_lookup"}


async def test_a_tool_the_parent_gated_is_gated_in_the_child() -> None:
    runner = _Runner()
    agent = Agent(
        tool_calling_model("start_session", {"agent": "research", "prompt": "L"}),
        toolsets=[_toolset(runner=runner), FunctionToolset([_lookup])],
        capabilities=[_GateLookup()],
    )
    deps = AgentDeps(user=_User(), call_context={"request": HttpRequest()})

    await agent.run("go", deps=deps, conversation_id="p1")
    lineage = _composed(runner.starts[0])[0]
    names = [("_lookup", "function"), ("_lookup", "output"), ("start_session", "function")]
    defs = [ToolDefinition(name=n, kind=k) for n, k in names]  # ty: ignore[invalid-argument-type]
    prepared = await lineage.prepare_tools(None, defs)  # ty: ignore[invalid-argument-type]

    assert lineage.gated == {"_lookup"}
    assert [(d.name, d.kind) for d in prepared] == [
        ("_lookup", "unapproved"),
        ("_lookup", "output"),
        ("start_session", "function"),
    ]


async def test_parallel_starts_read_the_index_one_after_another() -> None:
    index = _Index()
    runner = _Runner(index=index)
    calls = [ToolCallPart("start_session", {"agent": "research", "prompt": "L"}) for _ in range(4)]

    def model(messages: Any, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("ok")] if len(messages) > 1 else calls)

    agent = Agent(FunctionModel(model), toolsets=[_toolset(index=index, runner=runner)])
    deps = AgentDeps(user=_User(), call_context={"request": HttpRequest()})
    result = await agent.run("go", deps=deps, conversation_id="p1")

    returns = [
        p.content for m in result.all_messages() for p in m.parts if isinstance(p, ToolReturnPart)
    ]
    assert len(runner.starts) == 3
    assert [getattr(r, "refused", None) for r in returns].count("max_running_per_thread") == 1


@pytest.mark.parametrize("user", [None, _User(is_authenticated=False)])
async def test_an_anonymous_run_cannot_start_a_child(user: Any) -> None:
    runner = _Runner()
    agent = Agent(
        tool_calling_model("start_session", {"agent": "research", "prompt": "L"}),
        toolsets=[_toolset(runner=runner)],
    )
    deps = AgentDeps(user=user, call_context={"request": HttpRequest()})
    result = await agent.run("go", deps=deps, conversation_id="p1")

    [refusal] = [
        p.content for m in result.all_messages() for p in m.parts if isinstance(p, ToolReturnPart)
    ]
    assert refusal == ChildSessionRefusal("principal", "a child session needs a signed-in user")
    assert runner.starts == []


async def test_an_unknown_agent_is_refused() -> None:
    runner = _Runner()

    refusal = await _call(
        _toolset(runner=runner), "start_session", {"agent": "x", "prompt": "Look."}
    )

    assert refusal == ChildSessionRefusal("agent", "no agent 'x'; the agents are: research")
    assert runner.starts == []


@pytest.mark.parametrize(("depth", "refused"), [(1, False), (2, True)])
async def test_max_depth_refuses_a_third_level(depth: int, refused: bool) -> None:
    runner = _Runner()

    result = await _call(
        _toolset(runner=runner),
        "start_session",
        {"agent": "research", "prompt": "Look."},
        lineage=_lineage(depth),
    )

    assert isinstance(result, ChildSessionRefusal) is refused
    if refused:
        assert result.refused == "max_depth"
    else:
        lineage = _composed(runner.starts[0])[0]
        assert (lineage.root, lineage.depth) == (ThreadRef("root1", "main"), 2)


@pytest.mark.parametrize(
    ("statuses", "refused"),
    [((_RUNNING, _WAITING, _RUNNING), True), ((_RUNNING, _WAITING, _IDLE), False)],
)
async def test_three_live_children_per_thread_refuse_a_fourth(
    statuses: tuple[ThreadRunStatus, ...], refused: bool
) -> None:
    index = _Index({_HERE: [_child(f"c{i}", s) for i, s in enumerate(statuses)]})

    result = await _call(
        _toolset(index=index), "start_session", {"agent": "research", "prompt": "L"}
    )

    assert (getattr(result, "refused", None) == "max_running_per_thread") is refused
    assert isinstance(result, ChildSessionReceipt) is not refused


@pytest.mark.parametrize(("live", "refused"), [(9, False), (10, True)])
async def test_ten_live_children_per_user_refuse_another(live: int, refused: bool) -> None:
    result = await _call(
        _toolset(index=_Index(live=live)), "start_session", {"agent": "research", "prompt": "L"}
    )

    assert (getattr(result, "refused", None) == "max_running_per_user") is refused
    assert isinstance(result, ChildSessionReceipt) is not refused


@pytest.mark.parametrize(
    ("spent_scope", "refused"),
    [("main:p1", "child_sessions_tree_spend"), ("7", "child_sessions_day_spend")],
)
async def test_an_exhausted_budget_refuses(spent_scope: str, refused: str) -> None:
    store = _SpentStore(spent_scope)
    spend = ChildSessionSpend(store, tree_usd=Decimal("5"), day_usd=Decimal("5"))

    result = await _call(
        _toolset(spend=spend), "start_session", {"agent": "research", "prompt": "L"}
    )

    assert result == ChildSessionRefusal(refused, "the spend budget is exhausted")


async def test_budgets_with_room_let_the_child_start() -> None:
    spend = ChildSessionSpend(_SpentStore("other"), tree_usd=Decimal("5"), day_usd=Decimal("5"))

    result = await _call(
        _toolset(spend=spend), "start_session", {"agent": "research", "prompt": "L"}
    )

    assert isinstance(result, ChildSessionReceipt)


async def test_the_child_carries_the_tree_and_day_budgets() -> None:
    runner = _Runner()
    spend = ChildSessionSpend(InMemorySpendStore(), tree_usd=Decimal("5"), day_usd=Decimal("2"))

    await _call(
        _toolset(runner=runner, spend=spend),
        "start_session",
        {"agent": "research", "prompt": "L"},
        lineage=_lineage(1),
    )

    _, _, tree, day = _composed(runner.starts[0], spend)
    assert isinstance(tree, SpendLimits) and isinstance(day, SpendLimits)
    assert tree.store is day.store is spend.store
    [tree_budget], [day_budget] = tree.budgets, day.budgets
    assert (tree_budget.window, tree_budget.usd, tree_budget.scope(None)) == (
        "total",
        Decimal("5"),
        "main:root1",
    )
    assert (day_budget.window, day_budget.usd, day_budget.scope(None)) == ("day", Decimal("2"), "7")


async def test_cancel_takes_descendants_and_leaves_siblings() -> None:
    runner = _Runner()
    index = _Index(
        {
            _HERE: [_child("c1"), _child("c2")],
            ThreadRef("c1", "research"): [_child("g1"), _child("g2", _IDLE)],
            ThreadRef("g1", "research"): [_child("gg1")],
        }
    )

    result = await _call(_toolset(index=index, runner=runner), "cancel_task", {"thread_id": "c1"})

    assert result == "cancelled 4 running session(s)"
    assert runner.cancelled == ["gg1", "g1", "g2", "c1"]


@pytest.mark.parametrize("tool", ["cancel_task", "message_task", "answer_task", "check_task"])
async def test_a_thread_that_is_not_a_child_is_refused(tool: str) -> None:
    runner, queue = _Runner(), _Queue()
    index = _Index({_HERE: [_child("c1")], ThreadRef("c1", "research"): [_child("g1")]})
    args = {"thread_id": "g1", "message": "m", "answer": "a"}
    args = {k: v for k, v in args.items() if k == "thread_id" or tool.startswith(k[:3])}

    result = await _call(_toolset(index=index, runner=runner, queue=queue), tool, args)

    assert result == ChildSessionRefusal("thread_id", "'g1' is not a child of this conversation")
    assert runner.cancelled == [] and queue.sent == []


@pytest.mark.parametrize(
    ("tool", "arg", "kind"),
    [("message_task", "message", InboundKind.STEER), ("answer_task", "answer", InboundKind.ANSWER)],
)
async def test_message_and_answer_reach_the_childs_queue(
    tool: str, arg: str, kind: InboundKind
) -> None:
    queue = _Queue()
    index = _Index({_HERE: [_child("c1")]})

    result = await _call(
        _toolset(index=index, queue=queue), tool, {"thread_id": "c1", arg: "go on"}
    )

    assert result == "sent to c1"
    assert queue.sent == [(ThreadRef("c1", "research"), kind, {"text": "go on"}, _HERE)]


@pytest.mark.parametrize(
    "child",
    [
        ChildSession(
            ThreadRef("c1", "research"),
            _WAITING,
            (
                RunWaiting(
                    kind="question", summary="ask_parent", tool_call_id="q1", destructive=False
                ),
            ),
        ),
        ChildSession(
            ThreadRef("c1", "research"), ThreadRunStatus.FAILED, reason=RunEndReason.ERROR
        ),
        _child("c1", _IDLE),
    ],
)
async def test_check_task_shows_where_one_child_stands(child: ChildSession) -> None:
    index = _Index({_HERE: [_child("c0"), child]})

    result = await _call(_toolset(index=index), "check_task", {"thread_id": "c1"})

    assert result == child


async def test_list_tasks_lists_this_threads_children() -> None:
    index = _Index({_HERE: [_child("c1"), _child("c2", _IDLE)]})

    result = await _call(_toolset(index=index), "list_tasks")

    assert result == [_child("c1"), _child("c2", _IDLE)]


async def test_ask_parent_parks_the_child_on_a_question() -> None:
    queue = _Queue()

    output = await _call(
        _toolset(queue=queue), "ask_parent", {"question": "Which year?"}, lineage=_lineage(1)
    )

    assert isinstance(output, DeferredToolRequests)
    [call] = output.calls
    assert output.kind(call.tool_call_id) == "question"
    assert queue.sent == [(ThreadRef("p0"), InboundKind.QUESTION, {"text": "Which year?"}, _HERE)]


async def test_a_child_parked_on_ask_parent_is_recorded_waiting_on_a_question() -> None:
    output = await _call(_toolset(), "ask_parent", {"question": "Which year?"}, lineage=_lineage(1))

    [call] = output.calls
    waiting = build_run_waiting(output, call.tool_call_id, summary="ask_parent", destructive=False)
    assert waiting.kind == "question"


async def test_a_root_is_offered_start_session_with_the_agents_and_no_ask_parent() -> None:
    offered: dict[str, str | None] = {}

    def model(messages: Any, info: AgentInfo) -> ModelResponse:
        offered.update({tool.name: tool.description for tool in info.function_tools})
        return ModelResponse(parts=[TextPart("ok")])

    agent = Agent(FunctionModel(model), toolsets=[_toolset()])
    await agent.run("go", deps=AgentDeps(user=None))

    assert "ask_parent" not in offered
    assert (offered["start_session"] or "").endswith("- research: Finds things out.")


async def test_a_mount_with_task_control_is_refused() -> None:
    sub = SubAgent(Agent("test"), name="helper", description="helps")
    agent = Agent(
        "test",
        toolsets=[_toolset()],
        capabilities=[SubAgents(agents=[sub], task_control=TaskControl())],
    )

    with pytest.raises(UserError, match="list_tasks"):
        await agent.run("go", deps=AgentDeps(user=None))


async def test_a_run_without_its_request_is_refused() -> None:
    agent = Agent(tool_calling_model("list_tasks"), toolsets=[_toolset()])

    with pytest.raises(RuntimeError, match="call_context"):
        await agent.run("go", deps=AgentDeps(user=None), conversation_id="p1")


def test_the_toolset_without_the_harness_installed_is_refused_when_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "pydantic_ai_harness.sub_agents", None)

    with pytest.raises(ImportError):
        _toolset()


@dataclass
class _ComposingRunner(_Runner):
    queue: Any = None
    spend: ChildSessionSpend | None = None
    offered: list[tuple[str, str]] = field(default_factory=list)
    composed: list[Any] = field(default_factory=list)

    async def start(self, agent_id: str, prompt: str, principal: Any, **kwargs: Any) -> Any:
        started = await super().start(agent_id, prompt, principal, **kwargs)
        stored = Conversation(started.thread_id, parent=kwargs["parent"], lineage=kwargs["lineage"])
        self.composed = child_lineage_for(
            stored, agent_id=agent_id, owner=str(principal.pk), queue=self.queue, spend=self.spend
        )

        def model(messages: Any, info: AgentInfo) -> ModelResponse:
            self.offered.extend((t.name, t.kind) for t in info.function_tools)
            return ModelResponse(parts=[TextPart("found it")])

        child = Agent(FunctionModel(model), toolsets=[FunctionToolset([_lookup, _wider])])
        deps = AgentDeps(user=principal, call_context={"request": HttpRequest()})
        await child.run(
            prompt, deps=deps, conversation_id=started.thread_id, capabilities=self.composed
        )
        return started


def _wider() -> str:
    return "ran"  # pragma: no cover


@pytest.mark.filterwarnings("ignore::pydantic_ai_harness.spend_limits.UnpricedModelWarning")
async def test_a_runner_composing_from_lineage_alone_bounds_gates_and_reports_the_first_run() -> (
    None
):
    queue = _Queue()
    spend = ChildSessionSpend(InMemorySpendStore(), tree_usd=Decimal("5"), day_usd=Decimal("2"))
    runner = _ComposingRunner(queue=queue, spend=spend)
    agent = Agent(
        tool_calling_model("start_session", {"agent": "research", "prompt": "L"}),
        toolsets=[_toolset(runner=runner, queue=queue, spend=spend), FunctionToolset([_lookup])],
        capabilities=[_GateLookup()],
    )
    deps = AgentDeps(user=_User(), call_context={"request": HttpRequest()})

    await agent.run("go", deps=deps, conversation_id="p1")

    [start] = runner.starts
    assert start["capabilities"] == []
    assert ("_wider", "function") not in runner.offered
    assert ("_lookup", "unapproved") in runner.offered
    assert [c.id for c in runner.composed][2:] == [
        "child_sessions_tree_spend",
        "child_sessions_day_spend",
    ]
    [(parent, kind, body, sender)] = queue.sent
    assert (parent, kind, sender) == (_HERE, InboundKind.REPORT, ThreadRef("c-new", "research"))
    assert body["outcome"] == "ok" and "found it" in body["text"]


async def test_a_gated_tool_that_gates_itself_is_never_flipped_in_the_child() -> None:
    lineage = ChildLineage(
        root=_HERE, parent=_HERE, depth=1, tools=frozenset({"w"}), gated=frozenset({"w"})
    )
    defs = [ToolDefinition(name="w", metadata={SELF_GATED_METADATA_KEY: True})]

    prepared = await lineage.prepare_tools(None, defs)  # ty: ignore[invalid-argument-type]

    assert [(d.name, d.kind) for d in prepared] == [("w", "function")]
