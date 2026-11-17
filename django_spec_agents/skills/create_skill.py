from __future__ import annotations

from django.http import HttpRequest

from django_spec_agents.skills.types.skill_draft import SkillDraft
from django_spec_agents.skills.types.skill_policy import SkillPolicy
from django_spec_agents.skills.types.skill_t import SkillT
from django_spec_agents.skills.utils import apply_draft, clean_draft, require_room, write_skill


def create_skill(policy: SkillPolicy[SkillT], request: HttpRequest, draft: SkillDraft) -> SkillT:
    """Create a skill from `draft` in the scope `policy.new_skill` gives, if `can_write` admits it.

    Raises `InvalidSkillError` for a broken rule, `SkillLimitError` when the caller already sees
    `MAX_VISIBLE_SKILLS` enabled skills, and `SkillWriteRefusedError` when the policy refuses.
    """
    cleaned = clean_draft(draft)
    require_room(policy, request)
    skill = policy.new_skill(request)
    apply_draft(skill, cleaned)
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        skill.created_by = user
    return write_skill(policy, request, skill)
