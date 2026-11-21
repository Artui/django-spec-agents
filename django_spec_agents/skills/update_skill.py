from __future__ import annotations

from django.http import HttpRequest

from django_spec_agents.skills.skill_write_refused_error import SkillWriteRefusedError
from django_spec_agents.skills.types.skill_draft import SkillDraft
from django_spec_agents.skills.types.skill_policy import SkillPolicy
from django_spec_agents.skills.types.skill_t import SkillT
from django_spec_agents.skills.utils import (
    apply_draft,
    clean_draft,
    require_unflagged,
    require_unreserved,
    require_write,
    write_skill,
)


def update_skill(
    policy: SkillPolicy[SkillT], request: HttpRequest, skill: SkillT, draft: SkillDraft
) -> SkillT:
    """Replace `skill`'s name, description, body and flags with `draft`'s, if `can_write` admits it.

    `can_write` is asked twice, of the row as stored and of the result, so a policy can refuse
    both touching a skill and a change to it. Raises `InvalidSkillError` for a broken rule and
    `SkillWriteRefusedError` when the policy refuses; a refused update leaves both the row and
    `skill` as they were. A rename to a name the policy reserves through `ReservedSkillNames`
    is an `InvalidSkillError`; a skill keeping a name reserved since it was stored is not. So is
    a description or body the policy's `SkillScanning` scanner flags; the stored row is not scanned.
    """
    cleaned = clean_draft(draft)
    require_unflagged(policy, cleaned)
    if cleaned.name != skill.name:
        require_unreserved(policy, request, cleaned.name)
    require_write(policy, request, skill)
    before = SkillDraft(
        skill.name, skill.description, skill.body, skill.user_invocable, skill.model_invocable
    )
    apply_draft(skill, cleaned)
    try:
        return write_skill(policy, request, skill)
    except SkillWriteRefusedError:
        apply_draft(skill, before)
        raise
