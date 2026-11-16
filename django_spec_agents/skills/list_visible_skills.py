from __future__ import annotations

from django.http import HttpRequest

from django_spec_agents.skills.types.max_visible_skills import MAX_VISIBLE_SKILLS
from django_spec_agents.skills.types.skill_policy import SkillPolicy
from django_spec_agents.skills.types.skill_t import SkillT


def list_visible_skills(policy: SkillPolicy[SkillT], request: HttpRequest) -> list[SkillT]:
    """The caller's enabled skills, one per name: the first `policy.visible` yields wins.

    A disabled skill shadows nothing, so disabling a personal skill brings back the company
    one of the same name. The list stops at `MAX_VISIBLE_SKILLS`, in the policy's order.
    """
    seen: set[str] = set()
    skills: list[SkillT] = []
    for skill in policy.visible(request):
        if skill.enabled and skill.name not in seen:
            seen.add(skill.name)
            skills.append(skill)
            if len(skills) == MAX_VISIBLE_SKILLS:
                break
    return skills
