from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.tools import DeferredToolResults, ToolApproved

from django_spec_agents.policy.approvals.types.answered_by_key import ANSWERED_BY_KEY


def approved_by(tool_call_ids: Iterable[str], answered_by: str) -> DeferredToolResults | None:
    """Approve each call, marked ``answered_by`` on upstream's event and on the call's metadata."""
    ids = list(tool_call_ids)
    if not ids:
        return None
    approvals: dict[str, Any] = {tool_call_id: ToolApproved() for tool_call_id in ids}
    return DeferredToolResults(
        approvals=approvals,
        metadata={tool_call_id: {ANSWERED_BY_KEY: answered_by} for tool_call_id in ids},
        answered_by=dict.fromkeys(ids, answered_by),
    )


def model_label(model: Any) -> str:
    """``model`` as it reads after ``reviewer:``: the string, or a ``Model``'s ``system:name``."""
    if isinstance(model, str):
        return model
    return f"{model.system}:{model.model_name}"


def answered_by_mark(ctx: RunContext[Any]) -> str | None:
    """The mark an answerer left on this call's metadata, if any."""
    metadata = ctx.tool_call_metadata
    return metadata.get(ANSWERED_BY_KEY) if isinstance(metadata, dict) else None
