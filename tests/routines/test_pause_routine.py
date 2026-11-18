from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.contrib.auth.base_user import AbstractBaseUser
from django.http import HttpRequest

from django_spec_agents.routines import RoutineWriteRefusedError, pause_routine
from tests.routines.conftest import request_as
from tests.routines.sweeping import routine
from tests.routines.testapp.models import OwnRoutinesPolicy, TeamRoutine

DUE = datetime(2026, 11, 20, 8, tzinfo=UTC)


class RefusesAPausedRoutine(OwnRoutinesPolicy):
    def can_write(self, request: HttpRequest, routine: TeamRoutine) -> bool:
        return routine.enabled


def test_it_disables_the_routine_and_keeps_its_grant(member: AbstractBaseUser) -> None:
    held = routine(member, DUE, preapproved_tools=[{"name": "search", "destructive": False}])

    pause_routine(OwnRoutinesPolicy(), request_as(member), held)

    held.refresh_from_db()
    assert (held.enabled, held.next_fire_at) == (False, None)
    assert held.preapproved_tools == [{"name": "search", "destructive": False}]


def test_it_refuses_a_caller_the_policy_refuses(
    member: AbstractBaseUser, other: AbstractBaseUser
) -> None:
    held = routine(member, DUE)

    with pytest.raises(RoutineWriteRefusedError):
        pause_routine(OwnRoutinesPolicy(), request_as(other), held)

    held.refresh_from_db()
    assert (held.enabled, held.next_fire_at) == (True, DUE)


def test_a_refused_result_leaves_the_instance_as_it_was(member: AbstractBaseUser) -> None:
    held = routine(member, DUE)

    with pytest.raises(RoutineWriteRefusedError):
        pause_routine(RefusesAPausedRoutine(), request_as(member), held)

    assert (held.enabled, held.next_fire_at) == (True, DUE)
    held.refresh_from_db()
    assert held.enabled


class RefusesAnEnabledRoutine(OwnRoutinesPolicy):
    def can_write(self, request: HttpRequest, routine: TeamRoutine) -> bool:
        return not routine.enabled


def test_the_row_as_stored_is_asked_too(member: AbstractBaseUser) -> None:
    held = routine(member, DUE)

    with pytest.raises(RoutineWriteRefusedError):
        pause_routine(RefusesAnEnabledRoutine(), request_as(member), held)

    held.refresh_from_db()
    assert (held.enabled, held.next_fire_at) == (True, DUE)
