from __future__ import annotations

from collections.abc import Iterable

from django.contrib.auth.base_user import AbstractBaseUser
from django.db import models
from django.http import HttpRequest

from django_spec_agents.routines import AbstractRoutine, RoutinePolicy


class TeamRoutine(AbstractRoutine):
    """A routine scoped to a team, the stand-in for a consumer's tenant relation."""

    team = models.CharField(max_length=32)

    @classmethod
    def routine_policy(cls) -> RoutinePolicy[TeamRoutine]:
        return OwnRoutinesPolicy()


class OwnRoutinesPolicy:
    """A user sees and writes their own routines; staff may edit anyone's on the team."""

    def visible(self, request: HttpRequest) -> Iterable[TeamRoutine]:
        return TeamRoutine.objects.filter(principal=request.user)

    def can_write(self, request: HttpRequest, routine: TeamRoutine) -> bool:
        return routine.principal_id == request.user.pk or request.user.is_staff

    def can_run(self, principal: AbstractBaseUser, routine: TeamRoutine) -> bool:
        return principal.is_active and routine.enabled

    def new_routine(self, request: HttpRequest) -> TeamRoutine:
        return TeamRoutine(team="core")
