from __future__ import annotations

from datetime import UTC, datetime, time
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AnonymousUser

from django_spec_agents.policy.guard.tool_guard import ToolGuard
from django_spec_agents.routines import (
    MAX_ROUTINE_NAME_LENGTH,
    MAX_ROUTINE_PROMPT_LENGTH,
    MAX_ROUTINES_PER_USER,
    IntervalSchedule,
    InvalidRoutineError,
    RoutineWriteRefusedError,
    WeeklySchedule,
    create_routine,
    update_routine,
)
from tests.routines.conftest import draft, request_as, tool
from tests.routines.testapp.models import OwnRoutinesPolicy, TeamRoutine

# 2026-11-16 is a Monday; 14:00 UTC is 09:00 in New York, past the 08:00 firing.
MONDAY = datetime(2026, 11, 16, 14, 0, tzinfo=UTC)
TUESDAY_EIGHT = datetime(2026, 11, 17, 8, 0, tzinfo=ZoneInfo("America/New_York"))


@pytest.fixture
def at_monday() -> object:
    with mock.patch("django.utils.timezone.now", return_value=MONDAY) as now:
        yield now


def create(
    policy: OwnRoutinesPolicy, user: object, guard: ToolGuard, **changes: object
) -> TeamRoutine:
    return create_routine(policy, request_as(user), draft(**changes), guard=guard, tool_defs=[])


def test_a_create_runs_as_the_caller_in_the_policys_scope(
    policy: OwnRoutinesPolicy, member: AbstractBaseUser, guard: ToolGuard, at_monday: object
) -> None:
    routine = create(policy, member, guard)
    routine.refresh_from_db()

    assert (routine.principal, routine.created_by, routine.team) == (member, member, "core")
    assert routine.schedule == {"kind": "weekly", "weekdays": [0, 1, 2, 3, 4], "at": "08:00"}
    assert routine.get_schedule() == WeeklySchedule(weekdays=frozenset(range(5)), at=time(8, 0))
    assert routine.get_timezone() == ZoneInfo("America/New_York")
    assert routine.next_fire_at == TUESDAY_EIGHT
    assert (routine.enabled, routine.paused_reason, routine.last_run_id) == (True, "", None)


def test_a_disabled_routine_has_no_next_firing(
    policy: OwnRoutinesPolicy, member: AbstractBaseUser, guard: ToolGuard
) -> None:
    assert create(policy, member, guard, enabled=False).next_fire_at is None


def test_an_anonymous_caller_cannot_be_a_principal(
    policy: OwnRoutinesPolicy, guard: ToolGuard, db: None
) -> None:
    with pytest.raises(RoutineWriteRefusedError, match="authenticated"):
        create(policy, AnonymousUser(), guard)
    with pytest.raises(RoutineWriteRefusedError, match="authenticated"):
        create_routine(policy, request_as(None), draft(), guard=guard, tool_defs=[])
    assert not TeamRoutine.objects.exists()


def test_a_create_the_policy_refuses_saves_nothing(
    member: AbstractBaseUser, guard: ToolGuard
) -> None:
    refusing = mock.Mock(wraps=OwnRoutinesPolicy())
    refusing.can_write.return_value = False

    with pytest.raises(RoutineWriteRefusedError, match="Overdue digest"):
        create(refusing, member, guard)
    assert not TeamRoutine.objects.exists()


def test_a_user_holds_at_most_the_cap_and_others_are_unaffected(
    policy: OwnRoutinesPolicy,
    member: AbstractBaseUser,
    other: AbstractBaseUser,
    guard: ToolGuard,
) -> None:
    for index in range(MAX_ROUTINES_PER_USER - 1):
        create(policy, member, guard, name=f"r{index}")
    create(policy, member, guard, name="last", enabled=False)

    with pytest.raises(InvalidRoutineError, match=f"at most {MAX_ROUTINES_PER_USER} routines"):
        create(policy, member, guard, name="one too many")
    assert TeamRoutine.objects.filter(principal=member).count() == MAX_ROUTINES_PER_USER
    assert create(policy, other, guard).principal == other


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"name": "  "}, "needs a name"),
        ({"name": "x" * (MAX_ROUTINE_NAME_LENGTH + 1)}, "needs a name"),
        ({"agent_id": " "}, "names the agent"),
        ({"prompt": "\n"}, "needs a prompt"),
        ({"prompt": "x" * (MAX_ROUTINE_PROMPT_LENGTH + 1)}, "needs a prompt"),
        ({"timezone": "Mars/Olympus"}, "not an IANA timezone"),
        ({"timezone": "../etc/passwd"}, "not an IANA timezone"),
        ({"timezone": ""}, "not an IANA timezone"),
    ],
)
def test_a_draft_that_breaks_a_rule_is_refused(
    policy: OwnRoutinesPolicy,
    member: AbstractBaseUser,
    guard: ToolGuard,
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(InvalidRoutineError, match=message):
        create(policy, member, guard, **changes)


def test_a_draft_is_stripped_and_its_grant_deduplicated(
    policy: OwnRoutinesPolicy, member: AbstractBaseUser, guard: ToolGuard
) -> None:
    routine = create_routine(
        policy,
        request_as(member),
        draft(name=" Digest ", agent_id=" assistant ", prompt=" go ", preapproved_tools=("a", "a")),
        guard=guard,
        tool_defs=[tool("a")],
    )

    assert (routine.name, routine.agent_id, routine.prompt) == ("Digest", "assistant", "go")
    assert routine.preapproved_tools == [{"name": "a", "destructive": False}]


def test_an_update_never_moves_the_principal(
    policy: OwnRoutinesPolicy,
    member: AbstractBaseUser,
    staff: AbstractBaseUser,
    guard: ToolGuard,
) -> None:
    routine = create(policy, member, guard)

    update_routine(
        policy, request_as(staff), routine, draft(name="Renamed"), guard=guard, tool_defs=[]
    )
    routine.refresh_from_db()

    assert (routine.name, routine.principal, routine.created_by) == ("Renamed", member, member)


def test_an_update_the_policy_refuses_leaves_the_row(
    policy: OwnRoutinesPolicy,
    member: AbstractBaseUser,
    other: AbstractBaseUser,
    guard: ToolGuard,
) -> None:
    routine = create(policy, member, guard)

    with pytest.raises(RoutineWriteRefusedError):
        update_routine(
            policy, request_as(other), routine, draft(name="Mine"), guard=guard, tool_defs=[]
        )
    routine.refresh_from_db()
    assert routine.name == "Overdue digest"


def test_an_update_asks_can_write_of_the_result_too(
    member: AbstractBaseUser, guard: ToolGuard
) -> None:
    policy = OwnRoutinesPolicy()
    routine = create(policy, member, guard)
    refusing = mock.Mock(wraps=policy)
    refusing.can_write.side_effect = lambda request, row: row.name != "Forbidden"

    with pytest.raises(RoutineWriteRefusedError, match="Forbidden"):
        update_routine(
            refusing,
            request_as(member),
            routine,
            draft(name="Forbidden"),
            guard=guard,
            tool_defs=[],
        )
    assert TeamRoutine.objects.get().name == "Overdue digest"


def test_an_edit_that_keeps_the_schedule_keeps_the_next_firing(
    policy: OwnRoutinesPolicy, member: AbstractBaseUser, guard: ToolGuard
) -> None:
    routine = create(policy, member, guard, schedule=IntervalSchedule(every_hours=6))
    scheduled = routine.next_fire_at

    with mock.patch("django.utils.timezone.now", return_value=MONDAY):
        update_routine(
            policy,
            request_as(member),
            routine,
            draft(prompt="New", schedule=IntervalSchedule(every_hours=6)),
            guard=guard,
            tool_defs=[],
        )

    assert routine.next_fire_at == scheduled


LONDON_MONDAYS = {
    "timezone": "Europe/London",
    "schedule": WeeklySchedule(weekdays=frozenset({0}), at=time(8, 0)),
}


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        (
            {"schedule": WeeklySchedule(weekdays=frozenset({1}), at=time(8, 0))},
            datetime(2026, 11, 17, 8, 0, tzinfo=UTC),
        ),
        ({"timezone": "America/New_York"}, datetime(2026, 11, 23, 13, 0, tzinfo=UTC)),
    ],
)
def test_an_edit_to_the_schedule_or_zone_reschedules(
    policy: OwnRoutinesPolicy,
    member: AbstractBaseUser,
    guard: ToolGuard,
    at_monday: object,
    changes: dict[str, object],
    expected: datetime,
) -> None:
    routine = create(policy, member, guard, **LONDON_MONDAYS)
    assert routine.next_fire_at == datetime(2026, 11, 23, 8, 0, tzinfo=UTC)

    update_routine(
        policy,
        request_as(member),
        routine,
        draft(**{**LONDON_MONDAYS, **changes}),
        guard=guard,
        tool_defs=[],
    )

    assert routine.next_fire_at == expected


def test_disabling_clears_the_next_firing_and_enabling_clears_the_pause(
    policy: OwnRoutinesPolicy, member: AbstractBaseUser, guard: ToolGuard, at_monday: object
) -> None:
    routine = create(policy, member, guard)
    request = request_as(member)

    update_routine(policy, request, routine, draft(enabled=False), guard=guard, tool_defs=[])
    assert routine.next_fire_at is None
    routine.paused_reason = "authorization lapsed"
    update_routine(policy, request, routine, draft(enabled=False), guard=guard, tool_defs=[])
    assert routine.paused_reason == "authorization lapsed"
    update_routine(policy, request, routine, draft(), guard=guard, tool_defs=[])

    assert (routine.enabled, routine.paused_reason) == (True, "")
    assert routine.next_fire_at == TUESDAY_EIGHT


def test_an_edit_reenabling_a_paused_routine_reschedules_even_with_a_stale_firing(
    policy: OwnRoutinesPolicy, member: AbstractBaseUser, guard: ToolGuard, at_monday: object
) -> None:
    routine = create(policy, member, guard)
    routine.enabled = False
    routine.next_fire_at = datetime(2026, 1, 1, tzinfo=UTC)

    update_routine(policy, request_as(member), routine, draft(), guard=guard, tool_defs=[])

    assert routine.next_fire_at == TUESDAY_EIGHT


def test_an_edit_to_an_enabled_routine_with_no_firing_schedules_one(
    policy: OwnRoutinesPolicy, member: AbstractBaseUser, guard: ToolGuard, at_monday: object
) -> None:
    routine = create(policy, member, guard)
    routine.next_fire_at = None

    update_routine(policy, request_as(member), routine, draft(), guard=guard, tool_defs=[])

    assert routine.next_fire_at == TUESDAY_EIGHT


def test_the_next_firing_is_an_indexed_column_on_the_subclass() -> None:
    assert TeamRoutine._meta.get_field("next_fire_at").db_index is True
