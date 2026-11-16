from __future__ import annotations

from collections.abc import Iterable

from pydantic_ai.tools import ToolDefinition

from django_spec_agents.policy.guard.tool_guard import ToolGuard
from django_spec_agents.routines.invalid_routine_error import InvalidRoutineError
from django_spec_agents.routines.types.preapproved_tool import PreapprovedTool


def pin_preapproved_tools(
    guard: ToolGuard, tool_defs: Iterable[ToolDefinition], names: Iterable[str]
) -> tuple[PreapprovedTool, ...]:
    """Grant `names`, each pinned to `guard.requires_approval` of its definition as it is now.

    Raises `InvalidRoutineError` for a name none of `tool_defs` carries, so a typo is never
    recorded as a grant.
    """
    by_name = {tool_def.name: tool_def for tool_def in tool_defs}
    wanted = tuple(names)
    unknown = sorted(set(wanted) - set(by_name))
    if unknown:
        raise InvalidRoutineError(f"The agent offers no tool named {', '.join(unknown)}.")
    return tuple(
        PreapprovedTool(name=name, destructive=guard.requires_approval(by_name[name]))
        for name in wanted
    )
