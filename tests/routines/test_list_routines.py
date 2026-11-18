from __future__ import annotations

from collections.abc import Iterable

from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest

from django_spec_agents.routines import list_routines
from tests.routines.conftest import request_as
from tests.routines.sweeping import routine
from tests.routines.testapp.models import OwnRoutinesPolicy, TeamRoutine


class SeesTheTeam(OwnRoutinesPolicy):
    def visible(self, request: HttpRequest) -> Iterable[TeamRoutine]:
        return TeamRoutine.objects.order_by("pk")


def test_it_lists_the_callers_own_routines_disabled_ones_too(
    member: AbstractBaseUser, other: AbstractBaseUser
) -> None:
    first = routine(member, None, enabled=False)
    routine(other, None)
    second = routine(member, None, name="Second")

    assert list_routines(SeesTheTeam(), request_as(member)) == [first, second]


def test_an_anonymous_caller_has_none(member: AbstractBaseUser) -> None:
    routine(member, None)

    assert list_routines(SeesTheTeam(), request_as(AnonymousUser())) == []
    assert list_routines(SeesTheTeam(), request_as(None)) == []
