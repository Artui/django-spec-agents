from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

from django_spec_agents.policy.failure.types.tool_failure_config import ToolFailureConfig
from django_spec_agents.policy.failure.utils import PolicyToolFailed

_logger = logging.getLogger("django_pydantic_agent.failure")


class ToolFailurePolicy(AbstractCapability[Any]):
    """Turns an unhandled tool exception into a failed result the model can read."""

    def __init__(self, config: ToolFailureConfig | None = None) -> None:
        self._config = config if config is not None else ToolFailureConfig()
        self._reraise = (
            self._config.reraise if self._config.reraise is not None else _denial_types()
        )

    def get_ordering(self) -> CapabilityOrdering:
        """Pin the policy as an **outermost** capability."""
        return CapabilityOrdering(position="outermost")

    async def on_tool_execute_error(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: Any,
        error: Exception,
    ) -> Any:
        if isinstance(error, self._reraise):
            # Not logged here: the error reaches the transport intact; a traceback would lie.
            raise error
        if isinstance(error, (ModelRetry, ToolFailed)):
            raise error
        _logger.exception(
            "django-spec-agents: tool %r failed; the run continues with a failed result",
            tool_def.name,
            exc_info=error,
        )
        raise PolicyToolFailed(self._message(tool_def.name, error)) from error

    def _message(self, tool_name: str, error: Exception) -> str:
        """The model-facing text."""
        if self._config.include_detail:
            return f"The {tool_name} tool failed: {type(error).__name__}: {error}"
        return (
            f"The {tool_name} tool failed and returned no result. "
            "The failure has been recorded; do not retry the same call."
        )


def _denial_types() -> tuple[type[BaseException], ...]:
    """The default pass-through set: an authorization refusal, in each flavour."""
    denials: list[type[BaseException]] = [DjangoPermissionDenied]
    try:
        from django_service_specs import NotPermitted, PrincipalUnavailable
    except ImportError:
        pass
    else:
        denials.extend((NotPermitted, PrincipalUnavailable))
    return tuple(denials)


__all__ = ["ToolFailurePolicy"]
