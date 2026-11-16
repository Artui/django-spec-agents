from __future__ import annotations

from django.http import HttpRequest

from django_spec_agents.skills.invalid_skill_error import InvalidSkillError
from django_spec_agents.skills.list_visible_skills import list_visible_skills
from django_spec_agents.skills.types.max_visible_skills import MAX_VISIBLE_SKILLS
from django_spec_agents.skills.types.skill_draft import SkillDraft
from django_spec_agents.skills.types.skill_policy import SkillPolicy
from django_spec_agents.skills.types.skill_t import SkillT
from django_spec_agents.skills.utils import apply_draft, clean_draft, write_skill


def create_skill(policy: SkillPolicy[SkillT], request: HttpRequest, draft: SkillDraft) -> SkillT:
    """Create a skill from `draft` in the scope `policy.new_skill` gives, if `can_write` admits it.

    Raises `InvalidSkillError` for a broken rule, or when the caller already sees
    `MAX_VISIBLE_SKILLS` enabled skills, and `SkillWriteRefusedError` when the policy refuses.
    """
    cleaned = clean_draft(draft)
    if len(list_visible_skills(policy, request)) >= MAX_VISIBLE_SKILLS:
        raise InvalidSkillError(f"A caller sees at most {MAX_VISIBLE_SKILLS} enabled skills.")
    skill = policy.new_skill(request)
    apply_draft(skill, cleaned)
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        skill.created_by = user
    return write_skill(policy, request, skill)
