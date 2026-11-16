from __future__ import annotations

from django.http import HttpRequest

from django_spec_agents.skills.create_skill import create_skill
from django_spec_agents.skills.parse_skill_md import parse_skill_md
from django_spec_agents.skills.types.skill_policy import SkillPolicy
from django_spec_agents.skills.types.skill_t import SkillT


def import_skill_md(policy: SkillPolicy[SkillT], request: HttpRequest, text: str) -> SkillT:
    """Create a skill from a `SKILL.md`'s text, through `create_skill` and so `can_write`."""
    return create_skill(policy, request, parse_skill_md(text))
