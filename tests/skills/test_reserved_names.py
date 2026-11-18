from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace

import pytest
from django.contrib.auth.models import AbstractBaseUser
from django.http import HttpRequest
from pydantic_ai import ModelRetry

from django_spec_agents.skills import (
    InvalidSkillError,
    ReservedSkillNames,
    SkillDraft,
    build_skill_tools,
    create_skill,
    import_skill_md,
    update_skill,
)
from tests.skills.conftest import request_as
from tests.skills.testapp.models import PersonalFirstPolicy, TenantSkill

_TAKEN = "is taken by a built-in skill"
DRAFT = SkillDraft(name="summarize", description="Sum up.", body="Read, then sum up.")


class _ReservingPolicy(PersonalFirstPolicy):
    def reserved_names(self, request: HttpRequest) -> Collection[str]:
        return {"summarize"}


def test_a_policy_opts_in_by_defining_the_hook() -> None:
    assert isinstance(_ReservingPolicy(), ReservedSkillNames)
    assert not isinstance(PersonalFirstPolicy(), ReservedSkillNames)


def test_a_create_named_like_a_reserved_skill_is_refused(member: AbstractBaseUser) -> None:
    with pytest.raises(InvalidSkillError, match=_TAKEN):
        create_skill(_ReservingPolicy(), request_as(member), DRAFT)

    assert not TenantSkill.objects.exists()


def test_an_imported_skill_md_named_like_a_reserved_skill_is_refused(
    member: AbstractBaseUser,
) -> None:
    text = "---\nname: summarize\ndescription: Sum up.\n---\nRead, then sum up.\n"

    with pytest.raises(InvalidSkillError, match=_TAKEN):
        import_skill_md(_ReservingPolicy(), request_as(member), text)


def test_a_rename_to_a_reserved_name_is_refused_and_changes_nothing(
    member: AbstractBaseUser,
) -> None:
    policy, request = _ReservingPolicy(), request_as(member)
    skill = create_skill(policy, request, replace(DRAFT, name="triage"))

    with pytest.raises(InvalidSkillError, match=_TAKEN):
        update_skill(policy, request, skill, DRAFT)

    skill.refresh_from_db()
    assert skill.name == "triage"


def test_a_skill_keeping_a_name_reserved_since_it_was_stored_can_be_edited(
    member: AbstractBaseUser,
) -> None:
    request = request_as(member)
    skill = create_skill(PersonalFirstPolicy(), request, DRAFT)

    update_skill(_ReservingPolicy(), request, skill, replace(DRAFT, body="Sum up shorter."))

    skill.refresh_from_db()
    assert skill.body == "Sum up shorter."


def test_without_the_hook_a_reserved_looking_name_is_stored(member: AbstractBaseUser) -> None:
    assert create_skill(PersonalFirstPolicy(), request_as(member), DRAFT).pk is not None


@pytest.mark.django_db(transaction=True)
async def test_the_create_skill_tool_refuses_it_with_the_same_refusal(
    member: AbstractBaseUser,
) -> None:
    toolset = build_skill_tools(_ReservingPolicy(), request_as(member))

    with pytest.raises(ModelRetry, match=_TAKEN):
        await toolset.tools["create_skill"].function(
            name="summarize", description="Sum up.", body="Read."
        )
