from __future__ import annotations

from django.http import HttpRequest

from django_spec_agents.skills.invalid_skill_error import InvalidSkillError
from django_spec_agents.skills.list_visible_skills import list_visible_skills
from django_spec_agents.skills.skill_write_refused_error import SkillWriteRefusedError
from django_spec_agents.skills.types.max_visible_skills import MAX_VISIBLE_SKILLS
from django_spec_agents.skills.types.skill_policy import SkillPolicy
from django_spec_agents.skills.types.skill_t import SkillT
from django_spec_agents.skills.utils import require_write, write_skill


def enable_skill(policy: SkillPolicy[SkillT], request: HttpRequest, skill: SkillT) -> SkillT:
    """Turn `skill` back on if `can_write` admits the row and the result; a refusal changes nothing.

    Raises `InvalidSkillError` when the caller already sees `MAX_VISIBLE_SKILLS` enabled skills,
    as `create_skill` does, and `SkillWriteRefusedError` when the policy refuses.
    """
    require_write(policy, request, skill)
    if len(list_visible_skills(policy, request)) >= MAX_VISIBLE_SKILLS:
        raise InvalidSkillError(f"A caller sees at most {MAX_VISIBLE_SKILLS} enabled skills.")
    enabled, skill.enabled = skill.enabled, True
    try:
        return write_skill(policy, request, skill)
    except SkillWriteRefusedError:
        skill.enabled = enabled
        raise
