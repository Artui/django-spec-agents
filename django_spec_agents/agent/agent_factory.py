from __future__ import annotations

from typing import Any, cast

from pydantic_ai import Agent, DeferredToolRequests, Tool

from django_spec_agents.agent.types.agent_config import AgentConfig
from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.policy.audit.audit_capability import AuditCapability
from django_spec_agents.policy.audit.null_audit_logger import NullAuditLogger
from django_spec_agents.policy.failure.tool_failure_policy import ToolFailurePolicy
from django_spec_agents.policy.guard.tool_guard import ToolGuard
from django_spec_agents.registry.tool_registry import ToolRegistry
from django_spec_agents.registry.types.tool_spec import ToolSpec
from django_spec_agents.registry.utils import first_doc_paragraph


def build_agent(registry: ToolRegistry, config: AgentConfig) -> Agent[AgentDeps, Any]:
    """Build a Pydantic-AI ``Agent`` from a registry and an ``AgentConfig``."""
    capabilities = list(config.capabilities) if config.capabilities is not None else []
    if config.audit_logger is not None and not isinstance(config.audit_logger, NullAuditLogger):
        capabilities.append(
            AuditCapability(config.audit_logger, ip_address=config.audit_ip_address),
        )
    if config.tool_guard is not None and config.tool_guard.enabled:
        capabilities.append(ToolGuard(registry, config=config.tool_guard))
    if config.tool_failure.enabled:
        capabilities.insert(0, ToolFailurePolicy(config.tool_failure))
    return Agent(
        model=config.model,
        deps_type=AgentDeps,
        tools=[_pydantic_tool(binding.spec) for binding in registry],
        output_type=cast("Any", [str, DeferredToolRequests]),
        instructions=config.instructions,
        model_settings=cast("Any", config.model_settings),
        retries=config.retries,
        toolsets=list(config.toolsets) if config.toolsets is not None else None,
        capabilities=capabilities or None,
    )


def _pydantic_tool(spec: ToolSpec) -> Tool[AgentDeps]:
    """``spec`` as the tool the model is offered, under its registered name."""
    derived = spec.description == first_doc_paragraph(spec.fn)
    return Tool(spec.fn, name=spec.name, description=None if derived else spec.description)


__all__ = ["build_agent"]
