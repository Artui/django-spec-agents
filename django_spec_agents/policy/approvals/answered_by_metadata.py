from __future__ import annotations

import dataclasses
from typing import Any

from pydantic_ai import RunContext, ToolReturn
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

from django_spec_agents.policy.approvals.types.answered_by_key import ANSWERED_BY_KEY
from django_spec_agents.policy.approvals.utils import answered_by_mark


class AnsweredByMetadata(AbstractCapability[Any]):
    """Copies an answerer's ``answered_by`` mark onto the call's ``ToolReturnPart.metadata``.

    Upstream reports ``DeferredToolResults.answered_by`` only on its event, so the mark
    reaches the history here: from ``ctx.tool_call_metadata["answered_by"]`` to the result,
    as a ``ToolReturn`` whose dict ``metadata`` keeps the tool's own keys. A project's own
    handler gets the mark by setting the same key. A result whose metadata is not a dict
    is left alone. [`build_agent`][django_spec_agents.build_agent] always composes it.
    """

    async def after_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        result: Any,
    ) -> Any:
        """``result`` with the mark merged into its metadata, when the call carries one."""
        answered_by = answered_by_mark(ctx)
        if answered_by is None:
            return result
        if not isinstance(result, ToolReturn):
            return ToolReturn(return_value=result, metadata={ANSWERED_BY_KEY: answered_by})
        if result.metadata is not None and not isinstance(result.metadata, dict):
            return result
        metadata = {**(result.metadata or {}), ANSWERED_BY_KEY: answered_by}
        return dataclasses.replace(result, metadata=metadata)


__all__ = ["AnsweredByMetadata"]
