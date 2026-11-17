from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset

from django_spec_agents.agent.agent_factory import build_agent
from django_spec_agents.agent.types.agent_config import AgentConfig
from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.policy.approvals.answered_by_metadata import AnsweredByMetadata
from django_spec_agents.policy.approvals.types.approval_reviewer_config import (
    ApprovalReviewerConfig,
)
from django_spec_agents.policy.guard.tool_guard import ToolGuard
from django_spec_agents.policy.guard.types.tool_guard_config import ToolGuardConfig
from django_spec_agents.registry.decorator import tool
from django_spec_agents.registry.tool_registry import ToolRegistry
from tests.policy.approvals.approval_runs import Reviewer, parked, returns
from tests.policy.audit.capturing_audit_logger import CapturingAuditLogger


def double(n: int) -> int:
    """Double a number."""
    return n * 2


def test_build_agent_returns_agent_with_registry_tools() -> None:
    reg = ToolRegistry()
    tool(reg)(double)

    agent = build_agent(reg, AgentConfig(model=TestModel()))
    assert isinstance(agent, Agent)


def test_build_agent_puts_deferred_tool_requests_in_output_type() -> None:
    # Turns on the tool-approval interrupt loop for *server-side* tools.
    agent = build_agent(ToolRegistry(), AgentConfig(model=TestModel()))
    assert DeferredToolRequests in agent.output_type


def test_build_agent_accepts_model_settings_retries_toolsets_capabilities() -> None:
    reg = ToolRegistry()
    tool(reg)(double)

    agent = build_agent(
        reg,
        AgentConfig(
            model=TestModel(),
            model_settings={"temperature": 0.1},
            retries=2,
            toolsets=[FunctionToolset()],
            capabilities=[object()],
        ),
    )
    assert isinstance(agent, Agent)


def boom(n: int) -> int:
    """Always explodes."""
    raise ValueError("kaboom")


async def afetch(label: str) -> str:
    """Fetch something asynchronously."""
    return f"value:{label}"


async def aboom(label: str) -> str:
    """Async explosion."""
    raise RuntimeError("async kaboom")


# Plain functions stay sync: registry dispatch of a sync function is what the sync rows hold.
@pytest.mark.parametrize(
    ("fn", "error"),
    [
        (double, None),
        (boom, "kaboom"),
        (afetch, None),
        (aboom, "async kaboom"),
    ],
    ids=["sync-success", "sync-failure", "async-success", "async-failure"],
)
async def test_an_audited_tool_records_its_outcome(
    fn: Callable[..., Any], error: str | None
) -> None:
    reg = ToolRegistry()
    tool(reg)(fn)

    audit = CapturingAuditLogger()
    agent = build_agent(reg, AgentConfig(model=TestModel(), audit_logger=audit))
    await agent.run("go")

    [event] = audit.events
    assert event.tool_name == fn.__name__
    assert event.success is (error is None)
    if error is None:
        assert event.result_size is not None
    else:
        assert error in (event.error or "")


async def test_no_audit_logger_is_a_noop_default() -> None:
    reg = ToolRegistry()
    tool(reg)(double)

    # No audit_logger → NullAuditLogger; run must still succeed.
    agent = build_agent(reg, AgentConfig(model=TestModel()))
    result = await agent.run("double 4")
    assert result.output is not None


def test_tool_guard_is_composed_when_enabled() -> None:
    reg = ToolRegistry()

    @tool(reg, destructive=True)
    def drop_it(name: str) -> str:
        """Delete a thing."""
        return name

    agent = build_agent(
        reg,
        AgentConfig(model=TestModel(), tool_guard=ToolGuardConfig(enabled=True)),
    )
    root = agent.root_capability
    composed = getattr(root, "capabilities", [root])
    assert any(isinstance(c, ToolGuard) for c in composed)


def test_tool_guard_is_absent_when_disabled() -> None:
    agent = build_agent(
        ToolRegistry(),
        AgentConfig(model=TestModel(), tool_guard=ToolGuardConfig(enabled=False)),
    )
    root = agent.root_capability
    composed = getattr(root, "capabilities", [root])
    assert not any(isinstance(c, ToolGuard) for c in composed)


def _guarded(reviewer: ApprovalReviewerConfig | None) -> Agent[AgentDeps, Any]:
    reg = ToolRegistry()

    @tool(reg, destructive=True)
    def drop_it(name: str) -> str:
        """Delete a thing."""
        return name

    return build_agent(
        reg,
        AgentConfig(
            model=TestModel(call_tools=["drop_it"]),
            tool_guard=ToolGuardConfig(enabled=True),
            approval_reviewer=reviewer,
        ),
    )


async def test_an_approval_reviewer_answers_a_gated_call_in_the_run() -> None:
    reviewer = Reviewer("no")
    config = ApprovalReviewerConfig(model=reviewer.model(), question="Risky?", tools=["drop_it"])

    result = await _guarded(config).run("go", deps=AgentDeps(user=None))

    assert parked(result) == []
    assert returns(result)["drop_it"].metadata == {"answered_by": "reviewer:function:judge"}


async def test_without_an_approval_reviewer_a_gated_call_parks() -> None:
    agent = _guarded(None)
    result = await agent.run("go", deps=AgentDeps(user=None))

    assert parked(result) == ["drop_it"]
    composed = getattr(agent.root_capability, "capabilities", [agent.root_capability])
    assert any(isinstance(c, AnsweredByMetadata) for c in composed)
