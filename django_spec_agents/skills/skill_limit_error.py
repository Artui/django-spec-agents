from __future__ import annotations

from django_spec_agents.skills.invalid_skill_error import InvalidSkillError


class SkillLimitError(InvalidSkillError):
    """The caller already sees `MAX_VISIBLE_SKILLS` enabled skills, so no draft can be admitted."""
