from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django_service_specs import NotPermitted, ServiceSpec, SpecRegistry, kernel_spec
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from django_spec_agents.headless.types.firing_queue import FiringQueue
from django_spec_agents.headless.types.firing_state import FiringState
from django_spec_agents.headless.types.headless_runner import HeadlessRunner
from django_spec_agents.policy.guard.tool_guard import ToolGuard
from django_spec_agents.policy.guard.types.tool_guard_config import ToolGuardConfig
from django_spec_agents.registry.decorator import tool
from django_spec_agents.registry.tool_registry import ToolRegistry
from django_spec_agents.routines import ROUTINE_GRANT_KEY, authoring_toolset
from django_spec_agents.routines.run_agent_spec import run_agent_spec
from django_spec_agents.spec_tools import SpecToolset
from django_spec_agents.testing import slow_model, tool_calling_model
from tests.headless.task_worker import TaskWorker
from tests.routines.conftest import request_as
from tests.routines.firing import AgentRunner
from tests.routines.sweeping import routine
from tests.routines.testapp.models import OwnRoutinesPolicy, TeamRoutine
from tests.spec_tools.kernel_specs import DenyAll

pytestmark = pytest.mark.django_db(transaction=True)

FIRE = "run_routine"
_T0 = datetime(2026, 11, 18, tzinfo=UTC)


def _registry(ran: list[str]) -> ToolRegistry:
    registry = ToolRegistry()

    @tool(registry, destructive=True)
    def delete_order(order_id: int) -> str:
        """Delete an order."""
        ran.append("delete_order")
        return f"deleted {order_id}"

    @tool(registry)
    def archive_order(order_id: int) -> str:
        """Archive an order."""
        ran.append("archive_order")
        return f"archived {order_id}"

    return registry


def _worker(runner: Any, *, timeout: float = 30.0) -> TaskWorker:
    spec = run_agent_spec(TeamRoutine, runner, timeout=timeout)
    return TaskWorker({FIRE: spec}, lambda: _T0, lease=timedelta(minutes=5))


def _fire(worker: TaskWorker, firing: TeamRoutine, thread_id: str = "t-sweep") -> str:
    params = {"routine_id": firing.pk, "thread_id": thread_id}
    run_id = worker.enqueue(FIRE, params, actor=firing.principal)
    worker.run(run_id)
    return run_id


def _returns(messages: list[ModelMessage]) -> dict[str, ToolReturnPart]:
    return {
        part.tool_name: part
        for message in messages
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }


@pytest.fixture
def member(db: None) -> AbstractBaseUser:
    return get_user_model().objects.create_user(username="member")


def test_the_doubles_have_the_shapes_they_stand_in_for() -> None:
    assert isinstance(AgentRunner(model=None), HeadlessRunner)
    assert isinstance(_worker(AgentRunner(model=None)), FiringQueue)


def test_the_spec_declares_no_transaction_and_no_idempotence() -> None:
    spec = kernel_spec(run_agent_spec(TeamRoutine, AgentRunner(model=None), timeout=5.0))

    assert (spec.atomic, spec.idempotent) == (False, False)


def test_a_firing_runs_the_routines_prompt_as_its_principal_into_the_sweeps_thread(
    member: AbstractBaseUser,
) -> None:
    runner = AgentRunner(model=FunctionModel(lambda *_: ModelResponse([TextPart("ok")])))
    firing = routine(member, None, agent_id="support", prompt="What is overdue?")
    worker = _worker(runner, timeout=12.5)

    run_id = _fire(worker, firing)

    assert worker.rows[run_id].state == "SUCCEEDED"
    assert worker.rows[run_id].output == {
        "thread_id": "t-sweep",
        "run_id": "r1",
        "outcome": "completed",
        "waiting": [],
    }
    [call] = runner.calls
    assert (call.agent_id, call.prompt, call.timeout, call.thread_id) == (
        "support",
        "What is overdue?",
        12.5,
        "t-sweep",
    )
    assert call.principal.pk == member.pk
    assert (call.parent_run_id, call.routine_id) == (None, str(firing.pk))


def test_an_unlisted_gated_call_parks_and_the_firing_ends_succeeded_waiting(
    member: AbstractBaseUser,
) -> None:
    ran: list[str] = []
    runner = AgentRunner(model=tool_calling_model("delete_order", {"order_id": 4}))
    runner.registry = _registry(ran)
    worker = _worker(runner)

    run_id = _fire(worker, routine(member, None))

    row = worker.rows[run_id]
    assert row.state == "SUCCEEDED"
    assert row.output["outcome"] == "waiting"
    assert row.output["waiting"][0]["summary"] == "delete_order"
    assert worker.state(run_id) is FiringState.FINISHED
    assert ran == []


def test_a_preapproved_destructive_call_runs_and_is_recorded_on_the_thread(
    member: AbstractBaseUser,
) -> None:
    ran: list[str] = []
    runner = AgentRunner(model=tool_calling_model("delete_order", {"order_id": 4}))
    runner.registry = _registry(ran)
    grant = [
        {"name": "delete_order", "destructive": True},
        {"name": "archive_order", "destructive": False},
    ]
    firing = routine(member, None, preapproved_tools=grant)

    run_id = _fire(_worker(runner), firing)

    assert ran == ["delete_order"]
    messages = runner.threads["t-sweep"]
    part = _returns(messages)["delete_order"]
    assert part.metadata == {"answered_by": f"routine:{firing.pk}"}
    first = messages[0]
    assert isinstance(first, ModelRequest)
    assert first.metadata == {
        ROUTINE_GRANT_KEY: {
            "routine_id": firing.pk,
            "updated_at": firing.updated_at.isoformat(),
            "answered_by": f"routine:{firing.pk}",
            "tools": grant,
        }
    }
    assert [m.metadata for m in messages[1:] if isinstance(m, ModelRequest)] == [None]
    assert run_id


def test_a_tool_granted_ungated_and_destructive_since_parks(member: AbstractBaseUser) -> None:
    ran: list[str] = []
    runner = AgentRunner(model=tool_calling_model("delete_order", {"order_id": 4}))
    runner.registry = _registry(ran)
    firing = routine(
        member, None, preapproved_tools=[{"name": "delete_order", "destructive": False}]
    )
    worker = _worker(runner)

    run_id = _fire(worker, firing)

    assert worker.rows[run_id].output["outcome"] == "waiting"
    assert ran == []


def test_the_kernel_still_refuses_a_preapproved_tool_the_principal_may_not_call(
    member: AbstractBaseUser,
) -> None:
    ran: list[str] = []

    def wipe_orders(user: Any) -> dict[str, bool]:
        ran.append("wipe_orders")  # pragma: no cover
        return {"ok": True}  # pragma: no cover

    specs = SpecRegistry()
    specs.register(
        "wipe_orders", ServiceSpec(service=wipe_orders, permissions=[DenyAll()], atomic=False)
    )
    runner = AgentRunner(model=tool_calling_model("wipe_orders", {}), toolsets=[SpecToolset(specs)])
    firing = routine(member, None, preapproved_tools=[{"name": "wipe_orders", "destructive": True}])
    worker = _worker(runner)

    run_id = _fire(worker, firing)

    assert ran == []
    assert worker.rows[run_id].state == "SUCCEEDED"
    assert worker.rows[run_id].output["outcome"] == "error"


def test_a_firing_past_its_timeout_ends_timed_out(member: AbstractBaseUser) -> None:
    worker = _worker(AgentRunner(model=slow_model(3600.0)), timeout=0.05)

    run_id = _fire(worker, routine(member, None))

    assert worker.rows[run_id].state == "SUCCEEDED"
    assert worker.rows[run_id].output["outcome"] == "timed_out"


def test_the_firing_offers_no_routine_authoring_tools(member: AbstractBaseUser) -> None:
    offered: list[str] = []
    registry = ToolRegistry()
    authoring = authoring_toolset(
        OwnRoutinesPolicy(),
        request_as(member),
        agent_id="assistant",
        guard=ToolGuard(registry, config=ToolGuardConfig(enabled=True)),
    )

    @tool(registry)
    def search(query: str) -> str:
        """Search."""
        return query  # pragma: no cover

    def model(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("done")])

    runner = AgentRunner(model=FunctionModel(model), registry=registry, toolsets=[authoring])

    _fire(_worker(runner), routine(member, None))

    assert offered == ["search"]


def test_enqueue_refuses_a_routine_its_policy_may_not_run(member: AbstractBaseUser) -> None:
    worker = _worker(AgentRunner(model=None))
    firing = routine(member, None, enabled=False)

    with pytest.raises(NotPermitted):
        worker.enqueue(FIRE, {"routine_id": firing.pk, "thread_id": "t"}, actor=member)


def test_enqueue_refuses_an_actor_who_is_not_the_routines_principal(
    member: AbstractBaseUser,
) -> None:
    other = get_user_model().objects.create_user(username="other")
    worker = _worker(AgentRunner(model=None))
    firing = routine(member, None)

    with pytest.raises(NotPermitted):
        worker.enqueue(FIRE, {"routine_id": firing.pk, "thread_id": "t"}, actor=other)


def test_enqueue_refuses_a_routine_that_does_not_exist(member: AbstractBaseUser) -> None:
    worker = _worker(AgentRunner(model=None))

    with pytest.raises(NotPermitted):
        worker.enqueue(FIRE, {"routine_id": 999, "thread_id": "t"}, actor=member)


def test_a_routine_disabled_before_the_worker_lapses_the_firing(member: AbstractBaseUser) -> None:
    runner = AgentRunner(model=None)
    worker = _worker(runner)
    firing = routine(member, None)
    run_id = worker.enqueue(FIRE, {"routine_id": firing.pk, "thread_id": "t"}, actor=member)
    TeamRoutine.objects.filter(pk=firing.pk).update(enabled=False)

    worker.run(run_id)

    assert worker.state(run_id) is FiringState.LAPSED
    assert runner.calls == []


def test_the_check_asked_without_arguments_refuses(member: AbstractBaseUser) -> None:
    spec = kernel_spec(run_agent_spec(TeamRoutine, AgentRunner(model=None), timeout=5.0))
    [check] = spec.permissions
    firing = routine(member, None)

    assert check.has_permission(member, spec, arguments={"routine_id": firing.pk}) is True
    assert check.has_permission(member, spec, arguments=None) is False
    assert check.has_permission(None, spec, arguments={"routine_id": firing.pk}) is False
