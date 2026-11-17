from __future__ import annotations

from dataclasses import KW_ONLY, dataclass
from typing import Any

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import RunContext, ToolDefinition

from django_spec_agents.child_sessions.types.child_session_tool_names import (
    CHILD_SESSION_TOOL_NAMES,
)
from django_spec_agents.persistence.types.thread_ref import ThreadRef


@dataclass
class ChildLineage(AbstractCapability[Any]):
    """Where a child run sits in its tree, and the tools its parent had: composed into the child.

    ``depth`` counts from the root thread at 0. The child sees only the tools named in
    ``tools`` beside the child-session tools, so it is never wider than its parent; the
    child's own mount guards each call as it guards any.
    """

    root: ThreadRef
    parent: ThreadRef
    depth: int
    tools: frozenset[str]
    _: KW_ONLY
    id: str | None = "child_session_lineage"

    async def prepare_tools(
        self, ctx: RunContext[Any], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        allowed = self.tools | CHILD_SESSION_TOOL_NAMES
        return [tool_def for tool_def in tool_defs if tool_def.name in allowed]


__all__ = ["ChildLineage"]
