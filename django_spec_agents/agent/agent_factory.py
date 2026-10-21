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
    """Build a Pydantic-AI ``Agent`` from a registry and an
    [`AgentConfig`][django_spec_agents.AgentConfig].

    Each registry tool is registered as a plain Pydantic-AI tool, and ``config``
    supplies the model, the toolsets and capabilities composed alongside them,
    and the policies below. Frontend tools declared in the AG-UI
    ``RunAgentInput`` are merged by the adapter, not registered here.

    Three capabilities are added from ``config`` on request: an
    [`AuditCapability`][django_spec_agents.AuditCapability] that times and
    records **every** tool the agent runs — registry tools and composed toolsets
    alike — a [`ToolGuard`][django_spec_agents.ToolGuard] that flips
    destructive tools to require approval, and a
    [`ToolFailurePolicy`][django_spec_agents.ToolFailurePolicy], on unless
    turned off, so a raising tool fails its own call rather than the whole run.
    Each declares its position through ``get_ordering`` and pydantic-ai sorts
    them: audit innermost, so it records the tool's own execution, and the
    policy outermost, so it converts a failure only after every other
    capability has seen it. Within a tier list order breaks ties, so audit is
    appended after ``config.capabilities`` and the policy placed before them.
    What sorts after audit regardless, such as an innermost capability passed
    to a single run, can still reach the record; ``AuditCapability`` states
    that exception.

    **A destructive tool is not confirmed unless a config asks for it.** The
    approval interrupt exists only when ``config.tool_guard`` is enabled, and
    even then it reaches a tool only if that tool's source declares the mutation
    — a registry ``@tool(destructive=True)``, an MCP ``readOnlyHint`` of
    ``False``, an ``x-destructive`` schema stamp, or an explicit
    ``require_approval`` name. With no ``tool_guard``, every server-side tool
    here runs the moment the model calls it. The browser's own confirmation card
    is no substitute: it is driven by a client-registered tool's schema and never
    sees a tool that executes server-side. A transport describing this agent to
    the model should say which of the two it configured.

    The agent is typed ``Agent[AgentDeps, ...]``, so every run must be given
    [`AgentDeps`][django_spec_agents.AgentDeps] through ``deps=``.
    """
    capabilities = list(config.capabilities) if config.capabilities is not None else []
    if config.audit_logger is not None and not isinstance(config.audit_logger, NullAuditLogger):
        # Appended after ``config.capabilities``: audit is innermost, and list
        # order breaks ties within that tier, so this keeps its
        # ``before_tool_execute`` after every other innermost one's, such as
        # harness's guardrail and tool-call judge. The record then carries the
        # arguments after their rewrites, and a call they veto never reaches
        # audit's own ``before_tool_execute``.
        # ``test_the_arguments_are_the_ones_the_tool_received[innermost]``
        # fails without it from pydantic-ai 2.54.
        capabilities.append(
            AuditCapability(config.audit_logger, ip_address=config.audit_ip_address),
        )
    if config.tool_guard is not None and config.tool_guard.enabled:
        capabilities.append(ToolGuard(registry, config=config.tool_guard))
    if config.tool_failure.enabled:
        # Placed first: the policy is outermost, and first within that tier, so
        # its ``on_tool_execute_error`` runs after every other capability's.
        # Each of them, a step recorder included, sees the tool's exception
        # before the policy turns it into the model's redacted copy.
        # ``test_an_outermost_error_hook_still_sees_the_tools_exception`` fails
        # without it.
        capabilities.insert(0, ToolFailurePolicy(config.tool_failure))
    return Agent(
        model=config.model,
        deps_type=AgentDeps,
        tools=[_pydantic_tool(binding.spec) for binding in registry],
        # ``DeferredToolRequests`` turns on the approval interrupt loop for
        # *server-side* tools. The AG-UI adapter augments ``output_type`` only
        # when the run carries frontend tools, so a run whose only gated tool is
        # server-side would never defer; setting it here makes approval
        # independent of them, and pydantic-ai dedups when the adapter augments
        # too. Cast because ty cannot bind ``OutputDataT`` from a
        # heterogeneous list literal, so the call matches no overload and the
        # return degrades to unsolved typevars.
        output_type=cast("Any", [str, DeferredToolRequests]),
        instructions=config.instructions,
        # ``model_settings`` is a plain dict at the settings boundary; Agent
        # types it as the ``ModelSettings`` TypedDict.
        model_settings=cast("Any", config.model_settings),
        retries=config.retries,
        toolsets=list(config.toolsets) if config.toolsets is not None else None,
        capabilities=capabilities or None,
    )


def _pydantic_tool(spec: ToolSpec) -> Tool[AgentDeps]:
    """``spec`` as the tool the model is offered, under its registered name.

    The name is passed always: handed the bare function, pydantic-ai names the
    tool after it. The guard and the tool catalog key on ``spec.name``, so
    ``@tool(name=...)`` left the model calling a tool the guard never collected,
    and the audit record, which takes the name the model called, disagreed with
    the catalog.

    The description is passed only when it is not the one ``@tool`` derives
    from the docstring. ``ToolSpec.description`` is the first paragraph, and
    pydantic-ai's own extraction keeps more of the docstring than that, so passing it unconditionally would rewrite what the model reads
    for each docstringed tool nobody overrode. The cost is one blind spot: an
    explicit description equal to the first paragraph is indistinguishable from
    the derived one and the model gets the fuller extraction.
    ``test_an_override_equal_to_the_first_paragraph_is_not_told_apart_from_none``
    pins it, and ``test_the_model_is_offered_an_overriding_description`` holds
    that a differing override is passed. An empty override is passed too, but
    some pydantic-ai versions in the supported range read ``""`` as no
    description and fall back to the docstring, so it is not promised to empty
    the model's copy.
    """
    derived = spec.description == first_doc_paragraph(spec.fn)
    return Tool(spec.fn, name=spec.name, description=None if derived else spec.description)


__all__ = ["build_agent"]
