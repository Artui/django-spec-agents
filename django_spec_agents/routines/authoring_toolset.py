from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Literal, TypedDict

from asgiref.sync import sync_to_async
from django.http import HttpRequest
from pydantic import ConfigDict, with_config
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.tools import Tool, ToolDefinition
from pydantic_ai.toolsets.function import FunctionToolset

from django_spec_agents.policy.guard.tool_guard import ToolGuard
from django_spec_agents.routines.abstract_routine import AbstractRoutine
from django_spec_agents.routines.create_routine import create_routine
from django_spec_agents.routines.invalid_routine_error import InvalidRoutineError
from django_spec_agents.routines.list_routines import list_routines
from django_spec_agents.routines.parse_schedule import parse_schedule
from django_spec_agents.routines.pause_routine import pause_routine
from django_spec_agents.routines.routine_write_refused_error import RoutineWriteRefusedError
from django_spec_agents.routines.types.interval_schedule import IntervalSchedule
from django_spec_agents.routines.types.preapproved_tool import PreapprovedTool
from django_spec_agents.routines.types.routine_authoring_tools import ROUTINE_AUTHORING_TOOLS
from django_spec_agents.routines.types.routine_draft import RoutineDraft
from django_spec_agents.routines.types.routine_policy import RoutinePolicy
from django_spec_agents.routines.types.schedule import Schedule
from django_spec_agents.routines.update_routine import update_routine
from django_spec_agents.routines.utils import clean_draft, pin_grant, require_user, require_write
from django_spec_agents.types.x_confirm_key import X_CONFIRM_KEY

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_LIST = (
    "List this user's routines: the prompts that run on a schedule as them, with each one's "
    "id, agent, schedule, timezone, whether it is enabled and the tools it may call unasked."
)
_SCHEDULE = (
    " schedule is {kind: 'weekly', weekdays: [0-6, Monday is 0], at: 'HH:MM'} or "
    "{kind: 'interval', every_hours: N}; timezone is an IANA name such as 'Europe/Paris'. "
    "preapproved_tools names tools of yours a firing may call without asking; any other "
    "tool that needs approval parks the firing until the user answers."
)
_CREATE = (
    "Create a routine for this user: a short name and the prompt to run on this agent on a "
    "schedule." + _SCHEDULE + " The user approves it on a card that shows all of it."
)
_UPDATE = (
    "Replace a routine of this user's, by id from list_routines: send the whole routine, not "
    "only what changes; enabled=true resumes a paused one." + _SCHEDULE + " The user approves "
    "it on a card."
)
_PAUSE = "Pause a routine of this user's, by id, until an update enables it. The user approves."


@with_config(ConfigDict(title="WeeklySchedule"))
class _WeeklyArg(TypedDict):
    kind: Literal["weekly"]
    weekdays: list[int]
    at: str


@with_config(ConfigDict(title="IntervalSchedule"))
class _IntervalArg(TypedDict):
    kind: Literal["interval"]
    every_hours: int


_ScheduleArg = _WeeklyArg | _IntervalArg


def authoring_toolset(
    policy: RoutinePolicy[Any], request: HttpRequest, *, agent_id: str, guard: ToolGuard
) -> FunctionToolset[Any]:
    """The routine tools for a run on the agent `agent_id`, as the user `request` carries.

    `list_routines` lists the caller's own routines. `create_routine`, `update_routine` and
    `pause_routine` always ask the user first, whatever `guard` exempts and whatever an
    in-run answerer approves: each raises `ApprovalRequired` with an `x-confirm` card that
    names the routine's principal, prompt, schedule, timezone and every pre-approved tool,
    a destructive one named as such. A routine's principal is always the caller, its agent
    always `agent_id`, and an update or a pause reaches only a routine whose principal is the
    caller; every write still goes through the policy's `can_write`. Each granted name is
    pinned through `guard` against the run's own tools, client tools and these four left out.
    Compose it into a browser run only: a routine's firing hides these tools, and a headless
    run has nobody to show the card to.
    """

    async def list_tool() -> list[dict[str, Any]]:
        routines = await sync_to_async(list_routines, thread_sensitive=True)(policy, request)
        return [_listed(routine) for routine in routines]

    async def create(
        ctx: RunContext[Any],
        name: str,
        prompt: str,
        schedule: _ScheduleArg,
        timezone: str,
        preapproved_tools: list[str] | None = None,
    ) -> str:
        draft = _draft(name, agent_id, prompt, schedule, timezone, preapproved_tools, True)
        defs = _grantable(ctx)

        def write() -> str:
            if not ctx.tool_call_approved:
                cleaned = clean_draft(draft)
                user = require_user(request)
                grant = pin_grant(policy.new_routine(request), cleaned, guard, defs)
                _ask(f"Create the routine {cleaned.name!r}", user, cleaned, grant)
            routine = create_routine(policy, request, draft, guard=guard, tool_defs=defs)
            return f"Created the routine {routine.name!r}, id {routine.pk}."

        return await _run(write)

    async def update(
        ctx: RunContext[Any],
        routine_id: str,
        name: str,
        prompt: str,
        schedule: _ScheduleArg,
        timezone: str,
        preapproved_tools: list[str] | None = None,
        enabled: bool = True,
    ) -> str:
        draft = _draft(name, agent_id, prompt, schedule, timezone, preapproved_tools, enabled)
        defs = _grantable(ctx)

        def write() -> str:
            routine = _own(policy, request, routine_id)
            if routine.agent_id != agent_id:
                return (
                    f"The routine {routine_id} runs on the agent {routine.agent_id!r}; edit it "
                    "from a conversation with that agent."
                )
            if not ctx.tool_call_approved:
                require_write(policy, request, routine)
                cleaned = clean_draft(draft)
                grant = pin_grant(routine, cleaned, guard, defs)
                verb = f"Update the routine {routine.name!r} (id {routine.pk})"
                _ask(verb, routine.principal, cleaned, grant)
            update_routine(policy, request, routine, draft, guard=guard, tool_defs=defs)
            return f"Updated the routine {routine.name!r}, id {routine.pk}."

        return await _run(write)

    async def pause(ctx: RunContext[Any], routine_id: str) -> str:
        def write() -> str:
            routine = _own(policy, request, routine_id)
            if not ctx.tool_call_approved:
                require_write(policy, request, routine)
                card = (
                    f"Pause the routine {routine.name!r} (id {routine.pk}), which runs as "
                    f"{_who(routine.principal)} on the agent {routine.agent_id!r}?"
                )
                raise ApprovalRequired(metadata={X_CONFIRM_KEY: card})
            pause_routine(policy, request, routine)
            return f"Paused the routine {routine.name!r}, id {routine.pk}."

        return await _run(write)

    return FunctionToolset(
        [
            Tool(list_tool, name="list_routines", description=_LIST),
            Tool(create, name="create_routine", description=_CREATE),
            Tool(update, name="update_routine", description=_UPDATE),
            Tool(pause, name="pause_routine", description=_PAUSE),
        ],
        id="django-spec-agents-routine-tools",
    )


class _NotYours(Exception):
    """No routine of the caller's has the id the model sent."""


async def _run(write: Callable[[], str]) -> str:
    """Run `write` off the event loop; a broken draft is retried, a refusal reported."""
    try:
        return await sync_to_async(write, thread_sensitive=True)()
    except (_NotYours, RoutineWriteRefusedError) as error:
        return str(error)
    except InvalidRoutineError as error:
        raise ModelRetry(str(error)) from error


def _draft(
    name: str,
    agent_id: str,
    prompt: str,
    schedule: _ScheduleArg,
    timezone: str,
    preapproved_tools: list[str] | None,
    enabled: bool,
) -> RoutineDraft:
    """The draft the model's arguments describe; a schedule outside the vocabulary is retried."""
    try:
        parsed = parse_schedule(dict(schedule))
    except InvalidRoutineError as error:
        raise ModelRetry(str(error)) from error
    return RoutineDraft(
        name, agent_id, prompt, parsed, timezone, tuple(preapproved_tools or ()), enabled
    )


def _grantable(ctx: RunContext[Any]) -> list[ToolDefinition]:
    """The run's tools a firing could call: not the client's, and not these four."""
    manager = ctx.tool_manager
    defs = manager.tool_defs if manager is not None else []
    return [
        tool_def
        for tool_def in defs
        if tool_def.kind != "external" and tool_def.name not in ROUTINE_AUTHORING_TOOLS
    ]


def _own(policy: RoutinePolicy[Any], request: HttpRequest, routine_id: str) -> AbstractRoutine:
    """The caller's own routine with `routine_id`, or `_NotYours` for any other id."""
    for routine in list_routines(policy, request):
        if str(routine.pk) == str(routine_id):
            return routine
    raise _NotYours(f"This user has no routine with the id {routine_id}.")


def _ask(verb: str, principal: Any, draft: RoutineDraft, grant: Iterable[PreapprovedTool]) -> None:
    """Raise the approval the write waits on, its card naming everything the routine will do."""
    granted = [f"{t.name} (destructive, runs unasked)" if t.destructive else t.name for t in grant]
    card = (
        f"{verb}, run as {_who(principal)} on the agent {draft.agent_id!r}?\n"
        f"Prompt: {draft.prompt}\n"
        f"Schedule: {_schedule_text(draft.schedule)}, {draft.timezone}\n"
        f"Pre-approved tools: {', '.join(granted) if granted else 'none'}\n"
        f"Enabled: {'yes' if draft.enabled else 'no'}"
    )
    raise ApprovalRequired(metadata={X_CONFIRM_KEY: card})


def _who(principal: Any) -> str:
    return principal.get_username()


def _schedule_text(schedule: Schedule) -> str:
    if isinstance(schedule, IntervalSchedule):
        return f"every {schedule.every_hours} hours"
    days = ", ".join(_WEEKDAYS[day] for day in sorted(schedule.weekdays))
    return f"every {days} at {schedule.at:%H:%M}"


def _listed(routine: AbstractRoutine) -> dict[str, Any]:
    return {
        "id": str(routine.pk),
        "name": routine.name,
        "agent_id": routine.agent_id,
        "prompt": routine.prompt,
        "schedule": routine.schedule,
        "timezone": routine.timezone,
        "enabled": routine.enabled,
        "paused_reason": routine.paused_reason,
        "next_fire_at": routine.next_fire_at.isoformat() if routine.next_fire_at else None,
        "preapproved_tools": routine.preapproved_tools,
    }
