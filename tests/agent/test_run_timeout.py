from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from anthropic.types import Usage
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import RunCancelled, RunTimedOut
from pydantic_ai.messages import ModelMessage
from pydantic_ai.toolsets import FunctionToolset

from django_spec_agents.agent.agent_factory import build_agent
from django_spec_agents.agent.types.agent_config import AgentConfig
from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.policy.guard.types.tool_guard_config import ToolGuardConfig
from django_spec_agents.registry.tool_registry import ToolRegistry
from django_spec_agents.testing import slow_model, tool_calling_model
from tests.agent.anthropic_wire import anthropic_model, text_events
from tests.policy.audit.capturing_audit_logger import CapturingAuditLogger

_DEADLINE = 0.2


def _agent(model: Any, toolsets: list[Any] | None = None) -> Agent[AgentDeps, Any]:
    """An agent with every capability ``build_agent`` composes: audit, guard and failure policy."""
    config = AgentConfig(
        model=model,
        audit_logger=CapturingAuditLogger(),
        tool_guard=ToolGuardConfig(enabled=True),
        toolsets=toolsets,
    )
    return build_agent(ToolRegistry(), config)


async def _timed_out(agent: Agent[AgentDeps, Any], streamed: bool) -> RunTimedOut:
    """Run ``agent`` past its deadline and return the ``RunTimedOut`` it ended with."""
    deps = AgentDeps(user=None)
    with pytest.raises(RunTimedOut) as raised:
        if streamed:
            async with agent.run_stream_events("go", deps=deps, timeout=_DEADLINE) as stream:
                async for _ in stream:
                    pass
        else:
            await agent.run("go", deps=deps, timeout=_DEADLINE)
    return raised.value


def _shape(messages: list[ModelMessage]) -> list[tuple[str, list[tuple[str, Any]]]]:
    """Each message's kind and its parts' kinds and text, what a resume reads back."""
    return [
        (message.kind, [(part.part_kind, getattr(part, "content", None)) for part in message.parts])
        for message in messages
    ]


@pytest.mark.parametrize("streamed", [False, True])
async def test_a_run_past_its_deadline_ends_with_run_timed_out(streamed: bool) -> None:
    error = await _timed_out(_agent(slow_model(3600.0, ("partial ", "late"))), streamed)
    assert isinstance(error, RunCancelled)
    assert isinstance(error, TimeoutError)


@pytest.mark.parametrize("streamed", [False, True])
async def test_the_slow_double_times_out_as_a_stalled_provider_does(streamed: bool) -> None:
    usage = Usage(input_tokens=10, output_tokens=1)
    provider = anthropic_model([text_events("partial ", usage)], [], stall_after=3)
    double = slow_model(3600.0, ("partial ", "late"))
    real = await _timed_out(_agent(provider), streamed)
    doubled = await _timed_out(_agent(double), streamed)
    assert _shape(doubled.all_messages()) == _shape(real.all_messages())


async def test_a_streamed_run_keeps_the_partial_answer() -> None:
    error = await _timed_out(_agent(slow_model(3600.0, ("partial ", "late"))), streamed=True)
    assert _shape(error.all_messages()) == [
        ("request", [("user-prompt", "go")]),
        ("response", [("text", "partial ")]),
    ]


async def test_a_deadline_passing_in_a_tool_is_not_a_tool_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    toolset = FunctionToolset()

    @toolset.tool
    async def wait(ctx: RunContext[AgentDeps]) -> str:
        """Wait out the run."""
        await asyncio.sleep(3600)
        return "never"

    caplog.set_level(logging.ERROR)
    error = await _timed_out(_agent(tool_calling_model("wait"), [toolset]), streamed=False)
    assert isinstance(error, RunTimedOut)
    assert caplog.records == []


async def test_a_tool_reads_the_run_deadline() -> None:
    toolset = FunctionToolset()
    seen: list[float | None] = []

    @toolset.tool
    def remaining(ctx: RunContext[AgentDeps]) -> str:
        """Report the time left."""
        seen.append(ctx.remaining_time())
        return "ok"

    agent = _agent(tool_calling_model("remaining"), [toolset])
    await agent.run("go", deps=AgentDeps(user=None), timeout=60.0)
    (left,) = seen
    assert left is not None
    assert 0 < left <= 60.0
