from __future__ import annotations

from unittest import mock

import pytest
from django.contrib.auth.models import AbstractBaseUser

from django_spec_agents.skills import (
    MAX_VISIBLE_SKILLS,
    InvalidSkillError,
    SkillDraft,
    SkillWriteRefusedError,
    create_skill,
    disable_skill,
    enable_skill,
    resolve_skill,
)
from tests.skills.conftest import request_as
from tests.skills.testapp.models import PersonalFirstPolicy, TenantSkill

DRAFT = SkillDraft(name="triage", description="Sort the inbox.", body="Read, then sort.")


def test_enable_turns_a_skill_back_on_and_a_refused_enable_raises(
    policy: PersonalFirstPolicy, member: AbstractBaseUser, admin: AbstractBaseUser
) -> None:
    company = disable_skill(
        policy, request_as(admin), create_skill(policy, request_as(admin), DRAFT)
    )

    with pytest.raises(SkillWriteRefusedError):
        enable_skill(policy, request_as(member), company)
    company.refresh_from_db()
    assert company.enabled is False
    enable_skill(policy, request_as(admin), company)

    company.refresh_from_db()
    assert company.enabled is True
    assert resolve_skill(policy, request_as(member), "triage") == company


@pytest.mark.parametrize("refuse_enabled", [False, True], ids=["stored-row", "result"])
def test_an_enable_asks_about_the_stored_row_and_the_result(
    policy: PersonalFirstPolicy, member: AbstractBaseUser, refuse_enabled: bool
) -> None:
    skill = disable_skill(
        policy, request_as(member), create_skill(policy, request_as(member), DRAFT)
    )
    refusing = mock.Mock(wraps=policy)
    refusing.can_write.side_effect = lambda request, skill: skill.enabled is not refuse_enabled

    with pytest.raises(SkillWriteRefusedError):
        enable_skill(refusing, request_as(member), skill)

    assert skill.enabled is False
    skill.refresh_from_db()
    assert skill.enabled is False


def test_an_enable_past_the_visible_cap_is_refused(
    policy: PersonalFirstPolicy, member: AbstractBaseUser
) -> None:
    skill = disable_skill(
        policy, request_as(member), create_skill(policy, request_as(member), DRAFT)
    )
    TenantSkill.objects.bulk_create(
        TenantSkill(owner=member, name=f"s{index}", description="d", body="b")
        for index in range(MAX_VISIBLE_SKILLS)
    )

    with pytest.raises(InvalidSkillError, match=f"at most {MAX_VISIBLE_SKILLS}"):
        enable_skill(policy, request_as(member), skill)

    skill.refresh_from_db()
    assert skill.enabled is False
