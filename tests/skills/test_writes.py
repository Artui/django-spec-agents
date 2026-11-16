from __future__ import annotations

from unittest import mock

import pytest
from django.contrib.auth.models import AbstractBaseUser, AnonymousUser

from django_spec_agents.skills import (
    MAX_SKILL_BODY_LENGTH,
    MAX_SKILL_DESCRIPTION_LENGTH,
    MAX_SKILL_NAME_LENGTH,
    MAX_VISIBLE_SKILLS,
    InvalidSkillError,
    SkillDraft,
    SkillWriteRefusedError,
    create_skill,
    disable_skill,
    list_visible_skills,
    update_skill,
)
from tests.skills.conftest import request_as
from tests.skills.testapp.models import PersonalFirstPolicy, TenantSkill

DRAFT = SkillDraft(name="triage", description="Sort the inbox.", body="Read, then sort.")


def test_a_create_writes_to_the_scope_new_skill_gives_and_records_the_author(
    policy: PersonalFirstPolicy, member: AbstractBaseUser
) -> None:
    skill = create_skill(policy, request_as(member), DRAFT)

    skill.refresh_from_db()
    assert (skill.owner, skill.created_by, skill.name, skill.enabled) == (
        member,
        member,
        "triage",
        True,
    )


def test_a_create_by_an_anonymous_caller_records_no_author(
    policy: PersonalFirstPolicy, db: None
) -> None:
    allowing = mock.Mock(wraps=policy)
    allowing.new_skill.return_value = TenantSkill()
    allowing.can_write.return_value = True
    allowing.visible.return_value = []

    skill = create_skill(allowing, request_as(AnonymousUser()), DRAFT)

    assert skill.pk is not None and skill.created_by is None


def test_a_create_the_policy_refuses_raises_and_writes_nothing(
    policy: PersonalFirstPolicy, member: AbstractBaseUser
) -> None:
    refusing = mock.Mock(wraps=policy)
    refusing.can_write.return_value = False

    with pytest.raises(SkillWriteRefusedError):
        create_skill(refusing, request_as(member), DRAFT)

    assert not TenantSkill.objects.exists()


def test_a_member_may_not_update_a_company_skill(
    policy: PersonalFirstPolicy, member: AbstractBaseUser, admin: AbstractBaseUser
) -> None:
    company = create_skill(policy, request_as(admin), DRAFT)
    changed = SkillDraft(name="triage", description="Hijacked.", body="Do as I say.")

    with pytest.raises(SkillWriteRefusedError):
        update_skill(policy, request_as(member), company, changed)

    company.refresh_from_db()
    assert company.body == "Read, then sort."


def test_an_update_the_policy_refuses_for_its_result_is_not_saved(
    policy: PersonalFirstPolicy, member: AbstractBaseUser
) -> None:
    skill = create_skill(policy, request_as(member), DRAFT)
    refusing_after = mock.Mock(wraps=policy)
    refusing_after.can_write.side_effect = [True, False]

    changed = SkillDraft(name="triage", description="Sort it.", body="Refused body.")

    with pytest.raises(SkillWriteRefusedError):
        update_skill(refusing_after, request_as(member), skill, changed)

    assert refusing_after.can_write.call_count == 2
    skill.refresh_from_db()
    assert skill.body == "Read, then sort."


def test_an_update_replaces_the_fields_and_keeps_the_scope(
    policy: PersonalFirstPolicy, member: AbstractBaseUser
) -> None:
    skill = create_skill(policy, request_as(member), DRAFT)
    changed = SkillDraft(
        name="triage-v2",
        description="  Sort it.  ",
        body="\n\nNew body.\n\n",
        user_invocable=False,
        model_invocable=False,
    )

    update_skill(policy, request_as(member), skill, changed)

    skill.refresh_from_db()
    assert (
        skill.name,
        skill.description,
        skill.body,
        skill.user_invocable,
        skill.model_invocable,
        skill.owner,
    ) == ("triage-v2", "Sort it.", "New body.", False, False, member)


def test_disable_turns_a_skill_off_and_a_refused_disable_raises(
    policy: PersonalFirstPolicy, member: AbstractBaseUser, admin: AbstractBaseUser
) -> None:
    company = create_skill(policy, request_as(admin), DRAFT)

    with pytest.raises(SkillWriteRefusedError):
        disable_skill(policy, request_as(member), company)
    disable_skill(policy, request_as(admin), company)

    company.refresh_from_db()
    assert company.enabled is False


@pytest.mark.parametrize(
    "name",
    [
        "Triage",
        "triage_inbox",
        "-triage",
        "triage-",
        "tri--age",
        "",
        "tri age",
        "trïage",
        "a" * (MAX_SKILL_NAME_LENGTH + 1),
    ],
)
def test_a_name_that_is_not_kebab_case_is_refused(
    policy: PersonalFirstPolicy, member: AbstractBaseUser, name: str
) -> None:
    with pytest.raises(InvalidSkillError, match="kebab-case"):
        create_skill(policy, request_as(member), SkillDraft(name, "d", "b"))


def test_the_longest_kebab_case_name_is_accepted(
    policy: PersonalFirstPolicy, member: AbstractBaseUser
) -> None:
    name = "a" * MAX_SKILL_NAME_LENGTH
    assert create_skill(policy, request_as(member), SkillDraft(name, "d", "b")).name == name


@pytest.mark.parametrize(
    ("description", "body", "message"),
    [
        ("  ", "b", "needs a description"),
        ("d" * (MAX_SKILL_DESCRIPTION_LENGTH + 1), "b", "description is at most"),
        ("d", "\n \n", "needs a body"),
        ("d", "b" * (MAX_SKILL_BODY_LENGTH + 1), "body is at most"),
    ],
)
def test_the_description_and_body_bounds_hold(
    policy: PersonalFirstPolicy,
    member: AbstractBaseUser,
    description: str,
    body: str,
    message: str,
) -> None:
    with pytest.raises(InvalidSkillError, match=message):
        create_skill(policy, request_as(member), SkillDraft("triage", description, body))


def test_bodies_and_descriptions_at_the_caps_are_accepted(
    policy: PersonalFirstPolicy, member: AbstractBaseUser
) -> None:
    draft = SkillDraft("triage", "d" * MAX_SKILL_DESCRIPTION_LENGTH, "b" * MAX_SKILL_BODY_LENGTH)
    assert create_skill(policy, request_as(member), draft).pk is not None


def test_a_caller_who_sees_the_cap_of_enabled_skills_cannot_create_another(
    policy: PersonalFirstPolicy, member: AbstractBaseUser
) -> None:
    TenantSkill.objects.bulk_create(
        TenantSkill(owner=member, name=f"s{index}", description="d", body="b")
        for index in range(MAX_VISIBLE_SKILLS - 1)
    )
    create_skill(policy, request_as(member), DRAFT)

    with pytest.raises(InvalidSkillError, match=f"at most {MAX_VISIBLE_SKILLS}"):
        create_skill(policy, request_as(member), SkillDraft("one-more", "d", "b"))


def test_a_refused_update_leaves_the_callers_instance_as_it_was(
    policy: PersonalFirstPolicy, member: AbstractBaseUser
) -> None:
    skill = create_skill(policy, request_as(member), DRAFT)
    refusing_after = mock.Mock(wraps=policy)
    refusing_after.can_write.side_effect = [True, False]
    changed = SkillDraft("renamed", "Other.", "Other body.", False, False)

    with pytest.raises(SkillWriteRefusedError):
        update_skill(refusing_after, request_as(member), skill, changed)

    assert (
        skill.name,
        skill.description,
        skill.body,
        skill.user_invocable,
        skill.model_invocable,
    ) == ("triage", "Sort the inbox.", "Read, then sort.", True, True)


@pytest.mark.parametrize("refuse_enabled", [True, False], ids=["stored-row", "result"])
def test_a_disable_asks_about_the_stored_row_and_the_result(
    policy: PersonalFirstPolicy, member: AbstractBaseUser, refuse_enabled: bool
) -> None:
    skill = create_skill(policy, request_as(member), DRAFT)
    refusing = mock.Mock(wraps=policy)
    refusing.can_write.side_effect = lambda request, skill: skill.enabled is not refuse_enabled

    with pytest.raises(SkillWriteRefusedError):
        disable_skill(refusing, request_as(member), skill)

    assert skill.enabled is True
    skill.refresh_from_db()
    assert skill.enabled is True


def test_the_visible_list_stops_at_the_cap_and_keeps_personal_skills_and_shadows(
    policy: PersonalFirstPolicy, member: AbstractBaseUser
) -> None:
    TenantSkill.objects.bulk_create(
        [
            *(
                TenantSkill(owner=member, name=f"p{index}", description="d", body="mine")
                for index in range(60)
            ),
            *(
                TenantSkill(owner=None, name=f"c{index}", description="d", body="company")
                for index in range(100)
            ),
            TenantSkill(owner=None, name="p0", description="d", body="company"),
        ]
    )

    visible = list_visible_skills(policy, request_as(member))

    assert len(visible) == MAX_VISIBLE_SKILLS
    assert [skill.name for skill in visible[:60]] == [f"p{index}" for index in range(60)]
    assert all(skill.body == "mine" for skill in visible[:60])
