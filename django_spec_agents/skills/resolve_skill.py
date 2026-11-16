from __future__ import annotations

from django.http import HttpRequest

from django_spec_agents.skills.list_visible_skills import list_visible_skills
from django_spec_agents.skills.types.skill_policy import SkillPolicy
from django_spec_agents.skills.types.skill_t import SkillT


def resolve_skill(policy: SkillPolicy[SkillT], request: HttpRequest, name: str) -> SkillT | None:
    """The enabled skill the caller reaches by `name`, after shadowing, or `None`."""
    return next(
        (skill for skill in list_visible_skills(policy, request) if skill.name == name), None
    )
