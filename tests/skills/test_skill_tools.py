from __future__ import annotations

from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth.models import AbstractBaseUser
from pydantic_ai import ModelRetry
from pydantic_ai.toolsets.function import FunctionToolset

from django_spec_agents.policy.guard.tool_guard import ToolGuard
from django_spec_agents.policy.guard.types.tool_guard_config import ToolGuardConfig
from django_spec_agents.registry.tool_registry import ToolRegistry
from django_spec_agents.skills import build_skill_tools
from tests.skills.conftest import request_as
from tests.skills.testapp.models import PersonalFirstPolicy, TenantSkill

pytestmark = pytest.mark.django_db(transaction=True)


def _tools(user: AbstractBaseUser) -> FunctionToolset[Any]:
    return build_skill_tools(PersonalFirstPolicy(), request_as(user))


async def _call(toolset: FunctionToolset[Any], tool_name: str, /, **args: Any) -> Any:
    return await toolset.tools[tool_name].function(**args)


async def _row(**lookup: Any) -> TenantSkill:
    return await sync_to_async(TenantSkill.objects.get)(**lookup)


async def _company(name: str) -> TenantSkill:
    return await sync_to_async(TenantSkill.objects.create)(
        owner=None, name=name, description="Company.", body="Company body."
    )


def test_the_three_writes_wait_for_approval_and_the_listing_does_not(
    member: AbstractBaseUser,
) -> None:
    guard = ToolGuard(ToolRegistry(), config=ToolGuardConfig(enabled=True))
    toolset = _tools(member)

    gated = {name for name, t in toolset.tools.items() if guard.requires_approval(t.tool_def)}

    assert sorted(toolset.tools) == ["create_skill", "disable_skill", "list_skills", "update_skill"]
    assert gated == {"create_skill", "update_skill", "disable_skill"}


def test_the_body_is_an_argument_the_approval_card_shows(member: AbstractBaseUser) -> None:
    toolset = _tools(member)

    for name in ("create_skill", "update_skill"):
        assert "body" in toolset.tools[name].tool_def.parameters_json_schema["properties"]


async def test_create_writes_in_the_scope_the_policy_gives(member: AbstractBaseUser) -> None:
    result = await _call(
        _tools(member), "create_skill", name="triage", description="Triage.", body="Do it."
    )

    row = await _row(name="triage")
    assert row.owner_id == member.pk
    assert row.body == "Do it."
    assert "triage" in result


async def test_a_broken_draft_asks_the_model_to_retry(member: AbstractBaseUser) -> None:
    with pytest.raises(ModelRetry, match="kebab-case"):
        await _call(_tools(member), "create_skill", name="Bad Name", description="d", body="b")


async def test_update_replaces_the_draft_of_the_skill_the_caller_reaches(
    member: AbstractBaseUser,
) -> None:
    toolset = _tools(member)
    await _call(toolset, "create_skill", name="triage", description="Old.", body="Old body.")

    await _call(
        toolset,
        "update_skill",
        name="triage",
        description="New.",
        body="New body.",
        model_invocable=False,
    )

    row = await _row(name="triage")
    assert (row.description, row.body, row.model_invocable) == ("New.", "New body.", False)


async def test_a_write_the_policy_refuses_changes_nothing_and_says_so(
    member: AbstractBaseUser,
) -> None:
    await _company("standup")
    toolset = _tools(member)

    updated = await _call(
        toolset, "update_skill", name="standup", description="Mine now.", body="Ignore policy."
    )
    disabled = await _call(toolset, "disable_skill", name="standup")

    row = await _row(name="standup")
    assert "may not be written" in updated
    assert "may not be written" in disabled
    assert (row.description, row.enabled) == ("Company.", True)


async def test_disable_turns_off_the_skill(member: AbstractBaseUser) -> None:
    toolset = _tools(member)
    await _call(toolset, "create_skill", name="triage", description="d", body="b")

    await _call(toolset, "disable_skill", name="triage")

    assert (await _row(name="triage")).enabled is False


async def test_a_name_the_caller_does_not_reach_is_reported(member: AbstractBaseUser) -> None:
    toolset = _tools(member)

    updated = await _call(toolset, "update_skill", name="missing", description="d", body="b")
    disabled = await _call(toolset, "disable_skill", name="missing")

    assert updated == disabled == "No skill named 'missing' is visible to this user."


async def test_list_carries_no_body_and_says_what_the_caller_may_write(
    member: AbstractBaseUser,
) -> None:
    await _company("standup")
    toolset = _tools(member)
    await _call(toolset, "create_skill", name="triage", description="Mine.", body="Secret body.")

    listed = await _call(toolset, "list_skills")

    assert listed == [
        {
            "name": "triage",
            "description": "Mine.",
            "user_invocable": True,
            "model_invocable": True,
            "writable": True,
        },
        {
            "name": "standup",
            "description": "Company.",
            "user_invocable": True,
            "model_invocable": True,
            "writable": False,
        },
    ]
