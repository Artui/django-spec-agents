from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, replace
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.http import HttpRequest
from pydantic_ai.tools import ToolDefinition

from django_spec_agents.policy.guard.tool_guard import ToolGuard
from django_spec_agents.routines.abstract_routine import AbstractRoutine
from django_spec_agents.routines.invalid_routine_error import InvalidRoutineError
from django_spec_agents.routines.next_fire_at import next_fire_at
from django_spec_agents.routines.pin_preapproved_tools import pin_preapproved_tools
from django_spec_agents.routines.routine_write_refused_error import RoutineWriteRefusedError
from django_spec_agents.routines.schedule_to_json import schedule_to_json
from django_spec_agents.routines.types.max_routine_name_length import MAX_ROUTINE_NAME_LENGTH
from django_spec_agents.routines.types.max_routine_prompt_length import (
    MAX_ROUTINE_PROMPT_LENGTH,
)
from django_spec_agents.routines.types.preapproved_tool import PreapprovedTool
from django_spec_agents.routines.types.routine_draft import RoutineDraft
from django_spec_agents.routines.types.routine_policy import RoutinePolicy
from django_spec_agents.routines.types.routine_t import RoutineT


def zone(key: str) -> ZoneInfo:
    """The IANA zone `key` names; raises `InvalidRoutineError` for one `zoneinfo` cannot load."""
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
        raise InvalidRoutineError(f"{key!r} is not an IANA timezone.") from error


def clean_draft(draft: RoutineDraft) -> RoutineDraft:
    """`draft` stripped, its grant deduplicated; raises `InvalidRoutineError` on a broken rule."""
    cleaned = replace(
        draft,
        name=draft.name.strip(),
        agent_id=draft.agent_id.strip(),
        prompt=draft.prompt.strip(),
        preapproved_tools=tuple(dict.fromkeys(draft.preapproved_tools)),
    )
    if not cleaned.name or len(cleaned.name) > MAX_ROUTINE_NAME_LENGTH:
        raise InvalidRoutineError(
            f"A routine needs a name of at most {MAX_ROUTINE_NAME_LENGTH} characters."
        )
    if not cleaned.agent_id:
        raise InvalidRoutineError("A routine names the agent it runs.")
    if not cleaned.prompt or len(cleaned.prompt) > MAX_ROUTINE_PROMPT_LENGTH:
        raise InvalidRoutineError(
            f"A routine needs a prompt of at most {MAX_ROUTINE_PROMPT_LENGTH} characters."
        )
    zone(cleaned.timezone)
    return cleaned


def require_user(request: HttpRequest) -> object:
    """The caller's user, the only principal a routine takes; refuses an anonymous caller."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        raise RoutineWriteRefusedError("A routine is written by an authenticated user.")
    return user


def require_write(policy: RoutinePolicy[RoutineT], request: HttpRequest, routine: RoutineT) -> None:
    """Raise `RoutineWriteRefusedError` unless the policy's `can_write` admits `routine`."""
    if not policy.can_write(request, routine):
        raise RoutineWriteRefusedError(
            f"The routine {routine.name!r} may not be written by this caller."
        )


def apply_draft(
    routine: AbstractRoutine,
    draft: RoutineDraft,
    grant: Iterable[PreapprovedTool],
    now: datetime,
) -> None:
    """Copy a cleaned `draft` and its pinned `grant` onto `routine`, unsaved, and reschedule it."""
    schedule = schedule_to_json(draft.schedule)
    unchanged = (
        routine.enabled
        and routine.next_fire_at is not None
        and routine.schedule == schedule
        and routine.timezone == draft.timezone
    )
    routine.name = draft.name
    routine.agent_id = draft.agent_id
    routine.prompt = draft.prompt
    routine.schedule = schedule
    routine.timezone = draft.timezone
    routine.preapproved_tools = [asdict(tool) for tool in grant]
    if draft.enabled and not routine.enabled:
        routine.paused_reason = ""
    routine.enabled = draft.enabled
    if not draft.enabled:
        routine.next_fire_at = None
    elif not unchanged:
        routine.next_fire_at = next_fire_at(draft.schedule, zone(draft.timezone), now)


def pin_grant(
    routine: AbstractRoutine,
    names: tuple[str, ...],
    guard: ToolGuard,
    tool_defs: Iterable[ToolDefinition],
) -> tuple[PreapprovedTool, ...]:
    """The grant for `names`: the flag already pinned for a name `routine` held, a new pin else."""
    held = {tool.name: tool for tool in routine.get_preapproved_tools()}
    fresh = pin_preapproved_tools(guard, tool_defs, (name for name in names if name not in held))
    pinned = {tool.name: tool for tool in fresh}
    return tuple(held.get(name) or pinned[name] for name in names)
