from __future__ import annotations

from django_spec_agents.skills import AbstractSkill


class TenantSkillBase(AbstractSkill):
    """An abstract layer with no policy: never installed as a table, so never checked."""

    class Meta:
        abstract = True


class UnguardedSkill(TenantSkillBase):
    """Installed with no policy: the system check refuses it."""
