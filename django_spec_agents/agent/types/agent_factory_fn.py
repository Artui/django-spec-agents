from __future__ import annotations

from typing import Any, Protocol

from pydantic_ai import Agent

from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.registry.tool_registry import ToolRegistry


class AgentFactoryFn(Protocol):
    """The escape-hatch signature for a transport's ``agent_factory=`` argument."""

    def __call__(self, registry: ToolRegistry, config: Any) -> Agent[AgentDeps, Any]: ...


__all__ = ["AgentFactoryFn"]
