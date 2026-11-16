from __future__ import annotations

from typing import Self

from django.conf import settings
from django.db import models

from django_spec_agents.skills.types.max_skill_name_length import MAX_SKILL_NAME_LENGTH
from django_spec_agents.skills.types.skill_policy import SkillPolicy


class AbstractSkill(models.Model):
    """A user-written skill; the consumer subclasses it with its tenant relation and policy.

    The two flags mirror SKILL.md's `user-invocable` and `disable-model-invocation`.
    """

    name = models.CharField(max_length=MAX_SKILL_NAME_LENGTH)
    description = models.TextField()
    body = models.TextField()
    user_invocable = models.BooleanField(default=True)
    model_invocable = models.BooleanField(default=True)
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    @classmethod
    def skill_policy(cls) -> SkillPolicy[Self] | None:
        """The policy every read and write goes through; `None` fails the system check."""
        return None
