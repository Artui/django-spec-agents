from __future__ import annotations

from collections.abc import Iterable

from django.http import HttpRequest
from django.utils import timezone
from pydantic_ai.tools import ToolDefinition

from django_spec_agents.policy.guard.tool_guard import ToolGuard
from django_spec_agents.routines.invalid_routine_error import InvalidRoutineError
from django_spec_agents.routines.types.max_routines_per_user import MAX_ROUTINES_PER_USER
from django_spec_agents.routines.types.routine_draft import RoutineDraft
from django_spec_agents.routines.types.routine_policy import RoutinePolicy
from django_spec_agents.routines.types.routine_t import RoutineT
from django_spec_agents.routines.utils import (
    apply_draft,
    clean_draft,
    pin_grant,
    require_user,
    require_write,
)


def create_routine(
    policy: RoutinePolicy[RoutineT],
    request: HttpRequest,
    draft: RoutineDraft,
    *,
    guard: ToolGuard,
    tool_defs: Iterable[ToolDefinition],
) -> RoutineT:
    """Create a routine from `draft` in the scope `policy.new_routine` gives, run as the caller.

    The caller is the principal and `created_by`, and must be authenticated. Each granted tool
    is pinned through `guard` against `tool_defs`, the definitions the agent offers. Raises
    `InvalidRoutineError` for a broken rule, an unknown tool, or a caller who already holds
    `MAX_ROUTINES_PER_USER` routines, and `RoutineWriteRefusedError` when the policy refuses.
    """
    cleaned = clean_draft(draft)
    user = require_user(request)
    routine = policy.new_routine(request)
    if type(routine)._default_manager.filter(principal=user).count() >= MAX_ROUTINES_PER_USER:
        raise InvalidRoutineError(f"A user holds at most {MAX_ROUTINES_PER_USER} routines.")
    routine.principal = user
    routine.created_by = user
    grant = pin_grant(routine, cleaned, guard, tool_defs)
    apply_draft(routine, cleaned, grant, timezone.now())
    require_write(policy, request, routine)
    routine.save()
    return routine
