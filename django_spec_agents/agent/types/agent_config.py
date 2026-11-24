from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from django_spec_agents.policy.approvals.types.approval_reviewer_config import (
    ApprovalReviewerConfig,
)
from django_spec_agents.policy.audit.types.audit_logger import AuditLogger
from django_spec_agents.policy.failure.types.tool_failure_config import ToolFailureConfig
from django_spec_agents.policy.guard.types.tool_guard_config import ToolGuardConfig
from django_spec_agents.policy.loops.types.loop_detection_config import LoopDetectionConfig
from django_spec_agents.policy.spend.types.spend_limits_config import SpendLimitsConfig


@dataclass(frozen=True)
class AgentConfig:
    """Resolved construction parameters for a Pydantic-AI ``Agent``.

    Bundles everything [`build_agent`][django_spec_agents.build_agent]
    needs so the call site passes one record instead of a long keyword list.
    A transport resolves these from its own configuration and hands the record
    down; ``toolsets`` and ``capabilities`` arrive already resolved to instances
    (never dotted paths — this substrate resolves nothing from settings).
    """

    model: Any
    """The Pydantic-AI model (a model string or ``Model`` instance)."""

    instructions: str | None = None
    """System/instructions prompt for the agent."""

    audit_logger: AuditLogger | None = None
    """Wraps every server-side tool call for timing and success/failure
    records. ``None`` means no auditing."""

    audit_ip_address: str | None = None
    """Client IP stamped onto every audit event this agent records (the view
    fills it from the driving request). ``None`` leaves the field unset."""

    model_settings: dict[str, Any] | None = None
    """Pydantic-AI ``ModelSettings`` (temperature, max_tokens, …)."""

    retries: int | None = None
    """Default tool/output retry budget."""

    toolsets: Sequence[Any] | None = None
    """Extra Pydantic-AI toolsets composed alongside the registry tools."""

    capabilities: Sequence[Any] | None = None
    """Pydantic-AI capabilities passed to the ``Agent``."""

    tool_guard: ToolGuardConfig | None = None
    """Server-side destructive-tool approval policy. When set and ``enabled``,
    ``build_agent`` composes a
    [`ToolGuard`][django_spec_agents.ToolGuard] built from
    the registry's destructive tools; ``None`` or disabled leaves the agent
    ungated."""

    tool_failure: ToolFailureConfig = field(default_factory=ToolFailureConfig)
    """What an unhandled tool exception costs. On by default, so a raising tool
    fails its own call and the run carries on, with the exception still reaching
    the audit logger and the Python logger. A plain record rather than
    ``... | None`` because there is no third state, and ``None`` would have to
    mean "on" to keep that default — backwards next to ``tool_guard``."""

    approval_reviewer: ApprovalReviewerConfig | None = None
    """A second model that may approve a gated call in the person's place, through a
    [`ReviewedApprovals`][django_spec_agents.ReviewedApprovals] (the ``[harness]``
    extra). ``None``, the default, leaves every gated call to the person."""

    loop_detection: LoopDetectionConfig | None = None
    """Watches the run for repeated calls, results and text, through ``pydantic-ai-harness``'s
    ``DetectLoops`` (the ``[harness]`` extra). ``None``, the default, watches nothing."""

    spend_limits: SpendLimitsConfig | None = None
    """Prices each response and refuses the next request once a budget is spent, through
    ``pydantic-ai-harness``'s ``SpendLimits`` (the ``[harness]`` extra). ``None``, the
    default, counts nothing."""


__all__ = ["AgentConfig"]
