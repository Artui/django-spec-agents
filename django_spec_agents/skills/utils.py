from __future__ import annotations

import re
from dataclasses import replace

from django.http import HttpRequest

from django_spec_agents.skills.abstract_skill import AbstractSkill
from django_spec_agents.skills.invalid_skill_error import InvalidSkillError
from django_spec_agents.skills.list_visible_skills import list_visible_skills
from django_spec_agents.skills.skill_limit_error import SkillLimitError
from django_spec_agents.skills.skill_write_refused_error import SkillWriteRefusedError
from django_spec_agents.skills.types.max_skill_body_length import MAX_SKILL_BODY_LENGTH
from django_spec_agents.skills.types.max_skill_description_length import (
    MAX_SKILL_DESCRIPTION_LENGTH,
)
from django_spec_agents.skills.types.max_skill_name_length import MAX_SKILL_NAME_LENGTH
from django_spec_agents.skills.types.max_visible_skills import MAX_VISIBLE_SKILLS
from django_spec_agents.skills.types.skill_draft import SkillDraft
from django_spec_agents.skills.types.skill_policy import SkillPolicy
from django_spec_agents.skills.types.skill_t import SkillT

_KEBAB_CASE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def strip_blank_edges(text: str) -> str:
    """Join `text`'s lines with LF, minus blank leading and trailing lines, as the harness reads."""
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def clean_draft(draft: SkillDraft) -> SkillDraft:
    """`draft` normalized as the harness loads it; raises `InvalidSkillError` on a broken rule."""
    cleaned = replace(
        draft, description=draft.description.strip(), body=strip_blank_edges(draft.body)
    )
    if len(cleaned.name) > MAX_SKILL_NAME_LENGTH or not _KEBAB_CASE.fullmatch(cleaned.name):
        raise InvalidSkillError(
            f"Skill name {cleaned.name!r} must be kebab-case: lowercase ASCII letters and digits "
            f"in words joined by single hyphens, at most {MAX_SKILL_NAME_LENGTH} characters."
        )
    if not cleaned.description:
        raise InvalidSkillError("A skill needs a description.")
    if len(cleaned.description) > MAX_SKILL_DESCRIPTION_LENGTH:
        raise InvalidSkillError(
            f"A skill description is at most {MAX_SKILL_DESCRIPTION_LENGTH} characters."
        )
    if not cleaned.body:
        raise InvalidSkillError("A skill needs a body.")
    if len(cleaned.body) > MAX_SKILL_BODY_LENGTH:
        raise InvalidSkillError(f"A skill body is at most {MAX_SKILL_BODY_LENGTH} characters.")
    return cleaned


def require_write(policy: SkillPolicy[SkillT], request: HttpRequest, skill: SkillT) -> None:
    """Raise `SkillWriteRefusedError` unless the policy's `can_write` admits `skill`."""
    if not policy.can_write(request, skill):
        raise SkillWriteRefusedError(f"The skill {skill.name!r} may not be written by this caller.")


def require_room(policy: SkillPolicy[SkillT], request: HttpRequest) -> None:
    """Raise `SkillLimitError` when the caller already sees `MAX_VISIBLE_SKILLS` enabled skills."""
    if len(list_visible_skills(policy, request)) >= MAX_VISIBLE_SKILLS:
        raise SkillLimitError(f"A caller sees at most {MAX_VISIBLE_SKILLS} enabled skills.")


def write_skill(policy: SkillPolicy[SkillT], request: HttpRequest, skill: SkillT) -> SkillT:
    """Save `skill` once `require_write` admits it."""
    require_write(policy, request, skill)
    skill.save()
    return skill


def apply_draft(skill: AbstractSkill, draft: SkillDraft) -> None:
    """Copy a cleaned `draft`'s fields onto `skill`, unsaved."""
    skill.name = draft.name
    skill.description = draft.description
    skill.body = draft.body
    skill.user_invocable = draft.user_invocable
    skill.model_invocable = draft.model_invocable
