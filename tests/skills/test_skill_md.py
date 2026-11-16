from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from django.contrib.auth.models import AbstractBaseUser
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness.skills import SkillDefinition, Skills

from django_spec_agents.skills import (
    InvalidSkillError,
    SkillDraft,
    create_skill,
    export_skill_md,
    import_skill_md,
    parse_skill_md,
)
from tests.skills.conftest import request_as
from tests.skills.testapp.models import PersonalFirstPolicy, TenantSkill

TRICKY = "Quotes \" and ' , a colon: here, a # hash, tab\t, ünïcödé,   \x85 \x7f end"


def _fields(skill: TenantSkill | SkillDefinition) -> tuple[str, str, str, bool, bool]:
    return (
        skill.name,
        skill.description,
        skill.body,
        skill.user_invocable,
        skill.model_invocable,
    )


def _harness_load(text: str, name: str, tmp_path: Path) -> SkillDefinition:
    (tmp_path / name).mkdir()
    (tmp_path / name / "SKILL.md").write_text(text, encoding="utf-8")
    catalog = asyncio.run(Skills(".", workspace=LocalWorkspaceBackend(str(tmp_path))).load())
    assert catalog.skipped == ()
    (definition,) = catalog.skills
    return definition


@pytest.mark.parametrize(("user_invocable", "model_invocable"), [(True, False), (False, True)])
def test_a_skill_round_trips_through_skill_md_and_the_harness_reads_the_same_fields(
    policy: PersonalFirstPolicy,
    member: AbstractBaseUser,
    tmp_path: Path,
    user_invocable: bool,
    model_invocable: bool,
) -> None:
    draft = SkillDraft(
        name="sort-inbox",
        description=TRICKY,
        body="# Steps\n\n---\n\n1. Read: everything.\n   - indented\n",
        user_invocable=user_invocable,
        model_invocable=model_invocable,
    )
    skill = create_skill(policy, request_as(member), draft)
    text = export_skill_md(skill)

    imported = import_skill_md(policy, request_as(member), text)

    assert _fields(imported) == _fields(skill)
    assert _fields(_harness_load(text, "sort-inbox", tmp_path)) == _fields(skill)


def test_parse_reads_hand_written_plain_and_single_quoted_scalars() -> None:
    text = (
        "---\n"
        "# a comment\n"
        "name: triage\n"
        "\n"
        "description: 'It''s the triage skill'\n"
        "user-invocable: False\n"
        "disable-model-invocation: TRUE\n"
        "---\n"
        "\r\n\r\nBody line.\r\nSecond.\r\n\r\n"
    )

    assert parse_skill_md(text) == SkillDraft(
        name="triage",
        description="It's the triage skill",
        body="Body line.\nSecond.",
        user_invocable=False,
        model_invocable=False,
    )


def test_the_flags_default_as_the_harness_defaults_them() -> None:
    draft = parse_skill_md("---\nname: triage\ndescription: Sort it\n---\nBody\n")
    assert (draft.user_invocable, draft.model_invocable) == (True, True)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "starts with"),
        ("name: x\n---\n", "starts with"),
        ("---\nname: x\n", "no closing"),
        ("---\nname: x\nallowed-tools: Read\n---\nb\n", "'allowed-tools' is not supported"),
        ("---\nname: x\nname: y\n---\nb\n", "repeats 'name'"),
        ("---\nname: x\ndescription: >-\n---\nb\n", "needs quotes"),
        ("---\nname: x\ndescription:\n  - a\n---\nb\n", "one-line"),
        ("---\nname: x\ndescription a\n---\nb\n", "one-line"),
        ("---\nname: x\n---\nb\n", "no 'description'"),
        ("---\ndescription: d\n---\nb\n", "no 'name'"),
        ('---\nname: x\ndescription: "open\n---\nb\n', "not a closed quoted string"),
        ("---\nname: x\ndescription: 'it's'\n---\nb\n", "unescaped single quote"),
        ("---\nname: x\ndescription: a: b\n---\nb\n", "needs quotes"),
        ("---\nname: x\ndescription: a # b\n---\nb\n", "needs quotes"),
        ("---\nname: x\ndescription: d\nuser-invocable: yes\n---\nb\n", "true or false"),
        ("---\nname: Bad_Name\ndescription: d\n---\nb\n", "kebab-case"),
    ],
)
def test_parse_refuses_what_it_cannot_read_losslessly(text: str, message: str) -> None:
    with pytest.raises(InvalidSkillError, match=message):
        parse_skill_md(text)
