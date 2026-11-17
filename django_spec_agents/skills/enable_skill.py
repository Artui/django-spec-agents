from __future__ import annotations

from django.http import HttpRequest

from django_spec_agents.skills.skill_write_refused_error import SkillWriteRefusedError
from django_spec_agents.skills.types.skill_policy import SkillPolicy
from django_spec_agents.skills.types.skill_t import SkillT
from django_spec_agents.skills.utils import require_room, require_write, write_skill


def enable_skill(policy: SkillPolicy[SkillT], request: HttpRequest, skill: SkillT) -> SkillT:
    """Turn `skill` back on if `can_write` admits the row and the result; a refusal changes nothing.

    Raises `SkillLimitError` when the caller already sees `MAX_VISIBLE_SKILLS` enabled skills,
    as `create_skill` does, and `SkillWriteRefusedError` when the policy refuses.
    """
    require_write(policy, request, skill)
    require_room(policy, request)
    enabled, skill.enabled = skill.enabled, True
    try:
        return write_skill(policy, request, skill)
    except SkillWriteRefusedError:
        skill.enabled = enabled
        raise
