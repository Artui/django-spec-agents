from __future__ import annotations

import pytest
from django.contrib.auth.models import AbstractBaseUser
from django.http import HttpRequest

from django_spec_agents.skills import (
    InvalidSkillError,
    SkillDraft,
    SkillLimitError,
    create_skill,
    parse_skill_md,
)
from django_spec_agents.skills.types.max_skill_body_length import MAX_SKILL_BODY_LENGTH
from django_spec_agents.skills.types.max_skill_description_length import (
    MAX_SKILL_DESCRIPTION_LENGTH,
)
from django_spec_agents.skills.types.max_skill_name_length import MAX_SKILL_NAME_LENGTH
from django_spec_agents.skills.types.max_visible_skills import MAX_VISIBLE_SKILLS
from tests.skills.conftest import request_as
from tests.skills.testapp.models import PersonalFirstPolicy, TenantSkill


class _Reserving(PersonalFirstPolicy):
    def reserved_names(self, request: HttpRequest) -> set[str]:
        return {"summarize"}


def test_the_field_is_optional_and_defaults_to_none() -> None:
    assert InvalidSkillError("broken").field is None
    assert InvalidSkillError("broken", field="body").field == "body"
    assert str(InvalidSkillError("broken", field="body")) == "broken"


@pytest.mark.parametrize(
    ("draft", "field"),
    [
        (SkillDraft("Bad_Name", "d", "b"), "name"),
        (SkillDraft("a" * (MAX_SKILL_NAME_LENGTH + 1), "d", "b"), "name"),
        (SkillDraft("triage", "  ", "b"), "description"),
        (SkillDraft("triage", "d" * (MAX_SKILL_DESCRIPTION_LENGTH + 1), "b"), "description"),
        (SkillDraft("triage", "d", "\n \n"), "body"),
        (SkillDraft("triage", "d", "b" * (MAX_SKILL_BODY_LENGTH + 1)), "body"),
    ],
)
def test_a_broken_draft_names_the_field(
    member: AbstractBaseUser, draft: SkillDraft, field: str
) -> None:
    with pytest.raises(InvalidSkillError) as caught:
        create_skill(PersonalFirstPolicy(), request_as(member), draft)
    assert caught.value.field == field


def test_a_reserved_name_is_a_name_error(member: AbstractBaseUser) -> None:
    with pytest.raises(InvalidSkillError) as caught:
        create_skill(_Reserving(), request_as(member), SkillDraft("summarize", "d", "b"))
    assert caught.value.field == "name"


def test_the_visible_cap_has_no_single_field(member: AbstractBaseUser) -> None:
    for index in range(MAX_VISIBLE_SKILLS):
        TenantSkill.objects.create(
            name=f"skill-{index}", description="d", body="b", owner=member, enabled=True
        )
    with pytest.raises(SkillLimitError) as caught:
        create_skill(PersonalFirstPolicy(), request_as(member), SkillDraft("one-more", "d", "b"))
    assert caught.value.field is None


@pytest.mark.parametrize(
    ("text", "field"),
    [
        ("", None),
        ("---\nname: x\n", None),
        ("---\nname: x\nallowed-tools: Read\n---\nb\n", None),
        ("---\nname: x\nname: y\n---\nb\n", "name"),
        ("---\nname: x\ndescription: d\ndescription: e\n---\nb\n", "description"),
        ("---\nname: x\ndescription:\n  - a\n---\nb\n", None),
        ("---\nname: x\n---\nb\n", "description"),
        ("---\ndescription: d\n---\nb\n", "name"),
        ('---\nname: x\ndescription: "open\n---\nb\n', "description"),
        ('---\nname: "open\ndescription: d\n---\nb\n', "name"),
        ("---\nname: x\ndescription: 'it's'\n---\nb\n", "description"),
        ("---\nname: x\ndescription: a: b\n---\nb\n", "description"),
        ("---\nname: x\ndescription: d\nuser-invocable: yes\n---\nb\n", "user_invocable"),
        (
            "---\nname: x\ndescription: d\ndisable-model-invocation: yes\n---\nb\n",
            "model_invocable",
        ),
        ("---\nname: Bad_Name\ndescription: d\n---\nb\n", "name"),
        ("---\nname: x\ndescription: d\n---\n\n", "body"),
    ],
)
def test_a_skill_md_error_names_the_field(text: str, field: str | None) -> None:
    with pytest.raises(InvalidSkillError) as caught:
        parse_skill_md(text)
    assert caught.value.field == field
