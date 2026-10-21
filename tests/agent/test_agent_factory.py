from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset

from django_spec_agents.agent.agent_factory import build_agent
from django_spec_agents.agent.types.agent_config import AgentConfig
from django_spec_agents.policy.guard.tool_guard import ToolGuard
from django_spec_agents.policy.guard.types.tool_guard_config import ToolGuardConfig
from django_spec_agents.registry.decorator import tool
from django_spec_agents.registry.tool_registry import ToolRegistry
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
    # Turns on the tool-approval interrupt loop for *server-side* tools. The
    # AG-UI adapter only augments ``output_type`` with ``DeferredToolRequests``
    # when the run carries frontend tools, so a server-only gated tool would
    # otherwise crash the run with a RUN_ERROR ("a deferred tool call was
    # present, but DeferredToolRequests is not among output types"). Setting it
    # on the Agent makes the approval path independent of frontend tools.
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


# The sync tools stay sync on purpose: registry dispatch of a plain function is
# what the sync rows hold.
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
    # A raising tool does not end the run (the default tool-failure policy turns
    # it into a failed result); the operator's record still names the tool and
    # carries the real exception text.
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
