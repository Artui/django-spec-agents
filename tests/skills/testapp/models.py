from __future__ import annotations

from collections.abc import Iterable

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.http import HttpRequest

from django_spec_agents.skills import AbstractSkill, SkillPolicy


class TenantSkill(AbstractSkill):
    """A personal skill when `owner` is set, a company one when it is not."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE
    )

    @classmethod
    def skill_policy(cls) -> SkillPolicy[TenantSkill]:
        return PersonalFirstPolicy()


class PersonalFirstPolicy:
    """Members write personal skills, staff write company ones; a personal skill wins a name."""

    def visible(self, request: HttpRequest) -> Iterable[TenantSkill]:
        return TenantSkill.objects.filter(Q(owner=request.user) | Q(owner=None)).order_by(
            F("owner").asc(nulls_last=True), "pk"
        )

    def can_write(self, request: HttpRequest, skill: TenantSkill) -> bool:
        if skill.owner_id is None:
            return request.user.is_staff
        return skill.owner_id == request.user.pk

    def new_skill(self, request: HttpRequest) -> TenantSkill:
        return TenantSkill(owner=None if request.user.is_staff else request.user)
