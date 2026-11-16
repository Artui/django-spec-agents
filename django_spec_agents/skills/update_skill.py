from __future__ import annotations

from django.http import HttpRequest

from django_spec_agents.skills.types.skill_draft import SkillDraft
from django_spec_agents.skills.types.skill_policy import SkillPolicy
from django_spec_agents.skills.types.skill_t import SkillT
from django_spec_agents.skills.utils import apply_draft, clean_draft, require_write, write_skill


def update_skill(
    policy: SkillPolicy[SkillT], request: HttpRequest, skill: SkillT, draft: SkillDraft
) -> SkillT:
    """Replace `skill`'s name, description, body and flags with `draft`'s, if `can_write` admits it.

    `can_write` is asked twice, of the row as stored and of the result, so a policy can refuse
    both touching a skill and a change to it. Raises `InvalidSkillError` for a broken rule and
    `SkillWriteRefusedError` when the policy refuses; a refused update leaves the row unsaved.
    """
    cleaned = clean_draft(draft)
    require_write(policy, request, skill)
    apply_draft(skill, cleaned)
    return write_skill(policy, request, skill)
