from __future__ import annotations

from collections.abc import Iterable

from django.http import HttpRequest
from django.utils import timezone
from pydantic_ai.tools import ToolDefinition

from django_spec_agents.policy.guard.tool_guard import ToolGuard
from django_spec_agents.routines.types.routine_draft import RoutineDraft
from django_spec_agents.routines.types.routine_policy import RoutinePolicy
from django_spec_agents.routines.types.routine_t import RoutineT
from django_spec_agents.routines.utils import apply_draft, clean_draft, pin_grant, require_write


def update_routine(
    policy: RoutinePolicy[RoutineT],
    request: HttpRequest,
    routine: RoutineT,
    draft: RoutineDraft,
    *,
    guard: ToolGuard,
    tool_defs: Iterable[ToolDefinition],
) -> RoutineT:
    """Replace `routine`'s fields with `draft`'s, if `can_write` admits it; the principal stays.

    A tool the routine already granted keeps the flag pinned when it was granted, so an edit
    never re-approves a tool declared destructive since; only a newly named tool is pinned
    now. `next_fire_at` moves only when the schedule, the timezone or `enabled` changes.
    `can_write` is asked of the row as stored and of the result. Raises `InvalidRoutineError`
    for a broken rule or an unknown tool, and `RoutineWriteRefusedError` when the policy refuses.
    """
    cleaned = clean_draft(draft)
    require_write(policy, request, routine)
    grant = pin_grant(routine, cleaned.preapproved_tools, guard, tool_defs)
    apply_draft(routine, cleaned, grant, timezone.now())
    require_write(policy, request, routine)
    routine.save()
    return routine
