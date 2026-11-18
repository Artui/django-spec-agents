from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest
from pydantic_ai import DeferredToolRequests, DeferredToolResults, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import Tool, ToolDefinition
from pydantic_ai.toolsets.external import ExternalToolset
from pydantic_ai.toolsets.function import FunctionToolset
from pydantic_ai.usage import RunUsage

from django_spec_agents.agent.agent_factory import build_agent
from django_spec_agents.agent.types.agent_config import AgentConfig
from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.policy.guard.tool_guard import ToolGuard
from django_spec_agents.policy.guard.types.tool_guard_config import ToolGuardConfig
from django_spec_agents.registry.tool_registry import ToolRegistry
from django_spec_agents.routines import ROUTINE_AUTHORING_TOOLS, authoring_toolset
from django_spec_agents.types.destructive_metadata_key import DESTRUCTIVE_METADATA_KEY
from django_spec_agents.types.x_confirm_key import X_CONFIRM_KEY
from tests.routines.conftest import request_as
from tests.routines.sweeping import routine
from tests.routines.testapp.models import OwnRoutinesPolicy, TeamRoutine

pytestmark = pytest.mark.django_db(transaction=True)

WRITES = ("create_routine", "update_routine", "pause_routine")
WEEKLY = {"kind": "weekly", "weekdays": [0, 2], "at": "08:30"}


class SeesEveryoneWritesAnything(OwnRoutinesPolicy):
    """A policy that admits every read and write, so only the toolset can refuse."""

    def visible(self, request: HttpRequest) -> Iterable[TeamRoutine]:
        return TeamRoutine.objects.all()

    def can_write(self, request: HttpRequest, routine: TeamRoutine) -> bool:
        return True


class RefusesWrites(OwnRoutinesPolicy):
    def can_write(self, request: HttpRequest, routine: TeamRoutine) -> bool:
        return False


def send_email(to: str) -> str:
    return to  # pragma: no cover


def search(query: str) -> str:
    return query  # pragma: no cover


def _agent(user: Any, policy: Any = None, *, exempt: Iterable[str] = WRITES) -> Any:
    config = ToolGuardConfig(enabled=True, exempt=frozenset(exempt))
    tools = authoring_toolset(
        policy or OwnRoutinesPolicy(),
        request_as(user),
        agent_id="assistant",
        guard=ToolGuard(ToolRegistry(), config=config),
    )
    others = FunctionToolset(
        [
            Tool(send_email, metadata={DESTRUCTIVE_METADATA_KEY: True}),
            Tool(search),
        ]
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1].parts[-1]
        if isinstance(last, ToolReturnPart | RetryPromptPart) or not calls:
            return ModelResponse(parts=[TextPart(str(getattr(last, "content", "")))])
        name, args = calls.pop(0)
        return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id="c1")])

    agent = build_agent(
        ToolRegistry(),
        AgentConfig(model=FunctionModel(model), tool_guard=config, toolsets=[tools, others]),
    )
    return agent, calls


async def _call(user: Any, name: str, args: dict[str, Any], **options: Any) -> Any:
    agent, calls = _agent(user, **options)
    calls.append((name, args))
    return await agent.run("go", deps=AgentDeps(user=user))


async def _approve(user: Any, result: Any, **options: Any) -> Any:
    agent, _ = _agent(user, **options)
    return await agent.run(
        message_history=result.all_messages(),
        deferred_tool_results=DeferredToolResults(approvals={"c1": True}),
        deps=AgentDeps(user=user),
    )


def _create_args(**changes: Any) -> dict[str, Any]:
    return {
        "name": "Digest",
        "prompt": "tell me what is overdue",
        "schedule": WEEKLY,
        "timezone": "Europe/Paris",
        **changes,
    }


def _update_args(routine_id: Any, **changes: Any) -> dict[str, Any]:
    return {"routine_id": str(routine_id), **_create_args(**changes)}


def _card(result: Any) -> str:
    assert isinstance(result.output, DeferredToolRequests)
    [call] = result.output.approvals
    return result.output.metadata[call.tool_call_id][X_CONFIRM_KEY]


async def _rows() -> list[TeamRoutine]:
    return await sync_to_async(lambda: list(TeamRoutine.objects.order_by("pk")))()


async def _routine(principal: Any, **changes: Any) -> TeamRoutine:
    return await sync_to_async(routine)(principal, None, **changes)


async def test_create_waits_for_the_user_though_the_guard_exempts_it(
    member: AbstractBaseUser,
) -> None:
    waiting = await _call(member, "create_routine", _create_args())

    assert [c.tool_name for c in waiting.output.approvals] == ["create_routine"]
    assert await _rows() == []

    await _approve(member, waiting)

    [row] = await _rows()
    assert (row.name, row.agent_id, row.principal_id, row.timezone) == (
        "Digest",
        "assistant",
        member.pk,
        "Europe/Paris",
    )
    assert row.schedule == WEEKLY


async def test_update_waits_for_the_user_and_then_replaces_the_routine(
    member: AbstractBaseUser,
) -> None:
    held = await _routine(member)

    waiting = await _call(member, "update_routine", _update_args(held.pk, prompt="new prompt"))

    assert [c.tool_name for c in waiting.output.approvals] == ["update_routine"]
    assert (await _rows())[0].prompt == "tell me what is overdue"

    await _approve(member, waiting)

    assert (await _rows())[0].prompt == "new prompt"


async def test_pause_waits_for_the_user_and_then_disables_the_routine(
    member: AbstractBaseUser,
) -> None:
    kept = await _routine(member, name="Kept")
    held = await _routine(member)

    waiting = await _call(member, "pause_routine", {"routine_id": str(held.pk)})

    assert "Pause the routine 'Overdue digest'" in _card(waiting)
    assert "runs as member on the agent 'assistant'" in _card(waiting)
    assert all(r.enabled for r in await _rows())

    await _approve(member, waiting)

    rows = await _rows()
    assert [(r.pk, r.enabled, r.next_fire_at) for r in rows] == [
        (kept.pk, True, None),
        (held.pk, False, None),
    ]


async def test_the_listing_does_not_wait_and_lists_only_the_callers_own(
    member: AbstractBaseUser, other: AbstractBaseUser
) -> None:
    mine = await _routine(member, enabled=False)
    await _routine(other, name="Theirs")

    result = await _call(member, "list_routines", {}, policy=SeesEveryoneWritesAnything())

    [part] = [
        p
        for m in result.all_messages()
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    ]
    [listed] = part.content
    assert listed["id"] == str(mine.pk)
    assert (listed["name"], listed["enabled"], listed["next_fire_at"]) == (
        "Overdue digest",
        False,
        None,
    )


async def test_the_listing_gives_the_next_firing_as_an_iso_instant(
    member: AbstractBaseUser,
) -> None:
    held = await _routine(member, next_fire_at="2026-11-20T08:00:00Z")

    result = await _call(member, "list_routines", {})

    [part] = [
        p
        for m in result.all_messages()
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    ]
    assert part.content[0]["id"] == str(held.pk)
    assert part.content[0]["next_fire_at"] == "2026-11-20T08:00:00+00:00"


async def test_the_card_names_the_principal_prompt_schedule_and_each_destructive_grant(
    member: AbstractBaseUser,
) -> None:
    args = _create_args(preapproved_tools=["send_email", "search"])

    card = _card(await _call(member, "create_routine", args))

    assert card.splitlines() == [
        "Create the routine 'Digest', run as member on the agent 'assistant'?",
        "Prompt: tell me what is overdue",
        "Schedule: every Mon, Wed at 08:30, Europe/Paris",
        "Pre-approved tools: send_email (destructive, runs unasked), search",
        "Enabled: yes",
    ]


async def test_an_edit_to_the_grant_shows_the_whole_new_list_on_its_card(
    member: AbstractBaseUser,
) -> None:
    held = await _routine(member)
    args = _update_args(
        held.pk,
        schedule={"kind": "interval", "every_hours": 6},
        preapproved_tools=["send_email"],
        enabled=False,
    )

    card = _card(await _call(member, "update_routine", args))

    assert card.splitlines()[0] == (
        f"Update the routine 'Overdue digest' (id {held.pk}), run as member on the agent "
        "'assistant'?"
    )
    assert card.splitlines()[2:] == [
        "Schedule: every 6 hours, Europe/Paris",
        "Pre-approved tools: send_email (destructive, runs unasked)",
        "Enabled: no",
    ]


@pytest.mark.parametrize("name", ["update_routine", "pause_routine"])
async def test_a_routine_of_another_user_is_refused_though_the_policy_admits_it(
    member: AbstractBaseUser, other: AbstractBaseUser, name: str
) -> None:
    theirs = await _routine(other)
    args = _update_args(theirs.pk) if name == "update_routine" else {"routine_id": str(theirs.pk)}

    result = await _call(member, name, args, policy=SeesEveryoneWritesAnything())

    assert result.output == f"This user has no routine with the id {theirs.pk}."
    [row] = await _rows()
    assert (row.prompt, row.enabled) == ("tell me what is overdue", True)


async def test_a_created_routine_always_runs_as_the_caller(
    member: AbstractBaseUser,
) -> None:
    waiting = await _call(member, "create_routine", _create_args())

    await _approve(member, waiting)

    [row] = await _rows()
    assert (row.principal_id, row.created_by_id) == (member.pk, member.pk)


async def test_an_update_reaches_only_a_routine_on_this_agent(member: AbstractBaseUser) -> None:
    held = await _routine(member, agent_id="billing")

    result = await _call(member, "update_routine", _update_args(held.pk))

    assert result.output == (
        f"The routine {held.pk} runs on the agent 'billing'; edit it from a conversation with "
        "that agent."
    )


@pytest.mark.parametrize("name", ["update_routine", "pause_routine"])
async def test_a_write_the_policy_refuses_asks_nobody(member: AbstractBaseUser, name: str) -> None:
    held = await _routine(member)
    args = _update_args(held.pk) if name == "update_routine" else {"routine_id": str(held.pk)}

    result = await _call(member, name, args, policy=RefusesWrites())

    assert result.output == "The routine 'Overdue digest' may not be written by this caller."


async def test_an_anonymous_caller_is_refused_before_any_card() -> None:
    result = await _call(AnonymousUser(), "create_routine", _create_args())

    assert result.output == "A routine is written by an authenticated user."


@pytest.mark.parametrize(
    "grant",
    [["unknown_tool"], ["list_routines"], ["create_routine"]],
)
async def test_a_name_the_firing_cannot_call_is_retried_before_any_card(
    member: AbstractBaseUser, grant: list[str]
) -> None:
    result = await _call(member, "create_routine", _create_args(preapproved_tools=grant))

    assert result.output == f"The agent offers no tool named {grant[0]}."


async def test_a_client_tool_is_never_granted(member: AbstractBaseUser) -> None:
    agent, calls = _agent(member)
    calls.append(("create_routine", _create_args(preapproved_tools=["pick_file"])))
    result = await agent.run(
        "go",
        deps=AgentDeps(user=member),
        toolsets=[_external(ToolDefinition(name="pick_file", kind="external"))],
    )

    assert result.output == "The agent offers no tool named pick_file."


@pytest.mark.parametrize(
    ("schedule", "message"),
    [
        (
            {"kind": "weekly", "weekdays": [0, 0], "at": "08:00"},
            "A weekly schedule's weekdays are a list of integers without repeats.",
        ),
        ({"kind": "interval", "every_hours": 0}, "An interval is a whole number of hours"),
    ],
)
async def test_a_schedule_outside_the_vocabulary_is_retried(
    member: AbstractBaseUser, schedule: dict[str, Any], message: str
) -> None:
    result = await _call(member, "create_routine", _create_args(schedule=schedule))

    assert result.output.startswith(message)
    assert await _rows() == []


async def test_a_draft_broken_at_approval_is_retried_and_writes_nothing(
    member: AbstractBaseUser,
) -> None:
    result = await _call(member, "create_routine", _create_args(timezone="Mars/Olympus"))

    assert result.output == "'Mars/Olympus' is not an IANA timezone."
    assert await _rows() == []


async def test_with_no_tool_manager_nothing_can_be_granted(member: AbstractBaseUser) -> None:
    tools = authoring_toolset(
        OwnRoutinesPolicy(),
        request_as(member),
        agent_id="assistant",
        guard=ToolGuard(ToolRegistry(), config=ToolGuardConfig(enabled=True)),
    )
    ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage(), tool_call_approved=True)

    created = await tools.tools["create_routine"].function(ctx, **_create_args())

    [row] = await _rows()
    assert created == f"Created the routine 'Digest', id {row.pk}."
    assert row.preapproved_tools == []


def test_the_toolset_offers_exactly_the_authoring_tools() -> None:
    tools = authoring_toolset(
        OwnRoutinesPolicy(),
        request_as(None),
        agent_id="assistant",
        guard=ToolGuard(ToolRegistry(), config=ToolGuardConfig(enabled=True)),
    )

    assert set(tools.tools) == ROUTINE_AUTHORING_TOOLS


def _external(tool_def: ToolDefinition) -> ExternalToolset[Any]:
    return ExternalToolset([tool_def])
