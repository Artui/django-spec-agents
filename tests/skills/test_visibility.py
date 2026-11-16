from __future__ import annotations

from django.contrib.auth.models import AbstractBaseUser

from django_spec_agents.skills import SkillDraft, create_skill, list_visible_skills, resolve_skill
from tests.skills.conftest import request_as
from tests.skills.testapp.models import PersonalFirstPolicy, TenantSkill


def _draft(name: str, body: str) -> SkillDraft:
    return SkillDraft(name=name, description=f"{name} skill", body=body)


def test_a_personal_skill_shadows_a_company_one_of_the_same_name(
    policy: PersonalFirstPolicy, member: AbstractBaseUser, admin: AbstractBaseUser
) -> None:
    create_skill(policy, request_as(admin), _draft("triage", "Company triage."))
    create_skill(policy, request_as(admin), _draft("summarise", "Company summary."))
    create_skill(policy, request_as(member), _draft("triage", "My triage."))

    visible = list_visible_skills(policy, request_as(member))

    assert [(skill.name, skill.body) for skill in visible] == [
        ("triage", "My triage."),
        ("summarise", "Company summary."),
    ]
    resolved = resolve_skill(policy, request_as(member), "triage")
    assert resolved is not None and resolved.body == "My triage."
    resolved = resolve_skill(policy, request_as(admin), "triage")
    assert resolved is not None and resolved.body == "Company triage."


def test_a_disabled_personal_skill_brings_the_company_one_back(
    policy: PersonalFirstPolicy, member: AbstractBaseUser, admin: AbstractBaseUser
) -> None:
    create_skill(policy, request_as(admin), _draft("triage", "Company triage."))
    TenantSkill.objects.create(
        owner=member, name="triage", description="d", body="Mine.", enabled=False
    )

    resolved = resolve_skill(policy, request_as(member), "triage")

    assert resolved is not None and resolved.body == "Company triage."


def test_an_unknown_name_resolves_to_none(
    policy: PersonalFirstPolicy, member: AbstractBaseUser
) -> None:
    assert resolve_skill(policy, request_as(member), "missing") is None
