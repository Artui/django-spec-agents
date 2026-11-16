from __future__ import annotations

import json
from typing import Any

import pytest
from anthropic.types import Usage
from pydantic_ai.capabilities import Caching

from django_spec_agents.agent.agent_factory import build_agent
from django_spec_agents.agent.types.agent_config import AgentConfig
from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.policy.guard.types.tool_guard_config import ToolGuardConfig
from django_spec_agents.registry.decorator import tool
from django_spec_agents.registry.tool_registry import ToolRegistry
from tests.agent.anthropic_wire import anthropic_model, text_events, tool_use_events
from tests.policy.audit.capturing_audit_logger import CapturingAuditLogger

_PREFIX = ("tools", "system")


def double(n: int) -> int:
    """Double a number."""
    return n * 2


async def _run(bodies: list[dict[str, Any]], **config: Any) -> Any:
    """A tool call then an answer over the Anthropic wire, the second read from cache."""
    written = Usage(input_tokens=10, output_tokens=1, cache_creation_input_tokens=900)
    read = Usage(input_tokens=10, output_tokens=1, cache_read_input_tokens=900)
    model = anthropic_model(
        [tool_use_events("double", {"n": 2}, written), text_events("4", read)], bodies
    )
    registry = ToolRegistry()
    tool(registry)(double)
    agent = build_agent(
        registry,
        AgentConfig(
            model=model,
            instructions="Be terse.",
            audit_logger=CapturingAuditLogger(),
            tool_guard=ToolGuardConfig(enabled=True),
            **config,
        ),
    )
    return await agent.run("go", deps=AgentDeps(user=None))


@pytest.mark.parametrize(
    "config",
    [{"model_settings": {"cache": True}}, {"capabilities": [Caching()]}],
    ids=["model_settings", "capability"],
)
async def test_a_cached_run_marks_its_prefix_and_reports_cache_reads(config: Any) -> None:
    bodies: list[dict[str, Any]] = []
    result = await _run(bodies, **config)
    assert [body["tools"][-1]["cache_control"]["type"] for body in bodies] == ["ephemeral"] * 2
    assert [body["cache_control"]["type"] for body in bodies] == ["ephemeral"] * 2
    assert result.usage.cache_write_tokens == 900
    assert result.usage.cache_read_tokens == 900


async def test_the_composed_capabilities_keep_the_cached_prefix_stable() -> None:
    bodies: list[dict[str, Any]] = []
    await _run(bodies, model_settings={"cache": True})
    first, second = ({key: body[key] for key in _PREFIX} for body in bodies)
    assert first == second


async def test_an_uncached_run_sends_no_cache_marker() -> None:
    bodies: list[dict[str, Any]] = []
    await _run(bodies)
    assert all("cache_control" not in json.dumps(body) for body in bodies)
