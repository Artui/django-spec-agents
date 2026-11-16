from __future__ import annotations

from django.http import HttpRequest

from django_spec_agents.skills.skill_write_refused_error import SkillWriteRefusedError
from django_spec_agents.skills.types.skill_policy import SkillPolicy
from django_spec_agents.skills.types.skill_t import SkillT
from django_spec_agents.skills.utils import require_write, write_skill


def disable_skill(policy: SkillPolicy[SkillT], request: HttpRequest, skill: SkillT) -> SkillT:
    """Turn `skill` off if `can_write` admits the row and the result; a refusal changes nothing."""
    require_write(policy, request, skill)
    enabled, skill.enabled = skill.enabled, False
    try:
        return write_skill(policy, request, skill)
    except SkillWriteRefusedError:
        skill.enabled = enabled
        raise
