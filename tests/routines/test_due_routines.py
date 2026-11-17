from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from django.contrib.auth.base_user import AbstractBaseUser

from django_spec_agents.headless.types.firing_queue import FiringQueue
from django_spec_agents.headless.types.firing_state import FiringState
from django_spec_agents.routines import AbstractRoutine, RoutineSweep, due_routines
from tests.routines.sweeping import (
    EVERY_6_HOURS,
    Enqueued,
    RecordingQueue,
    routine,
    utc,
)
from tests.routines.testapp.models import TeamRoutine

# 2026-11-16 is a Monday.
MONDAY_8 = utc(2026, 11, 16, 8)
TUESDAY_8 = utc(2026, 11, 17, 8)
FIRE = "run_routine"


@pytest.fixture
def queue() -> RecordingQueue:
    return RecordingQueue()


def sweep(queue: RecordingQueue, **changes: object) -> RoutineSweep:
    return RoutineSweep(queue=queue, spec_name=FIRE, **changes)


def test_the_recording_queue_is_a_firing_queue(queue: RecordingQueue) -> None:
    assert isinstance(queue, FiringQueue)


def test_a_due_routine_is_enqueued_as_its_principal_and_moved_on(
    member: AbstractBaseUser, queue: RecordingQueue
) -> None:
    due = routine(member, MONDAY_8)

    result = due_routines(TeamRoutine, sweep(queue), now=MONDAY_8 + timedelta(minutes=1))

    due.refresh_from_db()
    assert queue.enqueued == [Enqueued(FIRE, {"routine_id": due.pk}, member)]
    assert result.fired == [due.pk]
    assert due.last_run_id == "run-1"
    assert due.next_fire_at == TUESDAY_8


def test_a_routine_not_yet_due_or_disabled_is_left_alone(
    member: AbstractBaseUser, queue: RecordingQueue
) -> None:
    later = routine(member, TUESDAY_8)
    off = routine(member, MONDAY_8, enabled=False)

    result = due_routines(TeamRoutine, sweep(queue), now=MONDAY_8)

    later.refresh_from_db()
    off.refresh_from_db()
    assert queue.enqueued == []
    assert result.fired == []
    assert (later.next_fire_at, off.next_fire_at) == (TUESDAY_8, MONDAY_8)


def test_a_sweep_run_twice_fires_once(member: AbstractBaseUser, queue: RecordingQueue) -> None:
    routine(member, MONDAY_8)

    due_routines(TeamRoutine, sweep(queue), now=MONDAY_8)
    second = due_routines(TeamRoutine, sweep(queue), now=MONDAY_8)

    assert len(queue.enqueued) == 1
    assert second.fired == []


def test_a_lapsed_firing_pauses_the_routine_with_its_reason(
    member: AbstractBaseUser, queue: RecordingQueue
) -> None:
    lapsed = routine(member, MONDAY_8, last_run_id="run-7")
    queue.states["run-7"] = FiringState.LAPSED

    result = due_routines(TeamRoutine, sweep(queue), now=MONDAY_8)

    lapsed.refresh_from_db()
    assert queue.enqueued == []
    assert result.paused == [lapsed.pk]
    assert (lapsed.enabled, lapsed.next_fire_at) == (False, None)
    assert lapsed.paused_reason.startswith("The last firing (run run-7) lapsed:")
    assert "lost the right to run it" in lapsed.paused_reason


def test_a_firing_still_pending_skips_this_one(
    member: AbstractBaseUser, queue: RecordingQueue
) -> None:
    busy = routine(member, MONDAY_8, last_run_id="run-7")
    queue.states["run-7"] = FiringState.PENDING

    result = due_routines(TeamRoutine, sweep(queue), now=MONDAY_8)

    busy.refresh_from_db()
    assert queue.enqueued == []
    assert result.busy == [busy.pk]
    assert (busy.enabled, busy.next_fire_at, busy.last_run_id) == (True, TUESDAY_8, "run-7")


@pytest.mark.parametrize("state", [FiringState.FINISHED, None])
def test_a_finished_or_forgotten_firing_lets_the_next_one_run(
    member: AbstractBaseUser, queue: RecordingQueue, state: FiringState | None
) -> None:
    done = routine(member, MONDAY_8, last_run_id="run-7")
    if state is not None:
        queue.states["run-7"] = state

    result = due_routines(TeamRoutine, sweep(queue), now=MONDAY_8)

    assert result.fired == [done.pk]


def test_a_principal_the_policy_refuses_pauses_the_routine(
    member: AbstractBaseUser, queue: RecordingQueue
) -> None:
    member.is_active = False
    member.save()
    refused = routine(member, MONDAY_8)

    result = due_routines(TeamRoutine, sweep(queue), now=MONDAY_8)

    refused.refresh_from_db()
    assert queue.enqueued == []
    assert result.paused == [refused.pk]
    assert (refused.enabled, refused.next_fire_at) == (False, None)
    assert refused.paused_reason == "Its principal may no longer run it."


def test_a_firing_missed_within_one_period_runs_once(
    member: AbstractBaseUser, queue: RecordingQueue
) -> None:
    late = routine(member, MONDAY_8)

    result = due_routines(TeamRoutine, sweep(queue), now=TUESDAY_8 - timedelta(minutes=1))

    late.refresh_from_db()
    assert result.fired == [late.pk]
    assert late.next_fire_at == TUESDAY_8


def test_a_firing_missed_past_one_period_is_skipped_and_recorded(
    member: AbstractBaseUser, queue: RecordingQueue, caplog: pytest.LogCaptureFixture
) -> None:
    late = routine(member, MONDAY_8)

    with caplog.at_level(logging.INFO, logger="django_spec_agents.routines.due_routines"):
        result = due_routines(TeamRoutine, sweep(queue), now=TUESDAY_8 + timedelta(minutes=1))

    late.refresh_from_db()
    assert queue.enqueued == []
    assert result.missed == [late.pk]
    assert late.next_fire_at == utc(2026, 11, 18, 8)
    assert f"Routine {late.pk} missed its firing" in caplog.text


def test_the_default_grace_is_the_gap_to_the_next_firing(
    member: AbstractBaseUser, queue: RecordingQueue
) -> None:
    friday = routine(member, utc(2026, 11, 20, 8))

    result = due_routines(TeamRoutine, sweep(queue), now=utc(2026, 11, 22, 20))

    friday.refresh_from_db()
    assert result.fired == [friday.pk]
    assert friday.next_fire_at == utc(2026, 11, 23, 8)


def test_a_grace_given_replaces_the_period(member: AbstractBaseUser, queue: RecordingQueue) -> None:
    late = routine(member, MONDAY_8)
    strict = sweep(queue, grace=timedelta(minutes=10))

    on_time = due_routines(TeamRoutine, strict, now=MONDAY_8 + timedelta(minutes=10))
    late.next_fire_at = TUESDAY_8
    late.save()
    missed = due_routines(TeamRoutine, strict, now=TUESDAY_8 + timedelta(minutes=11))

    assert (on_time.fired, missed.missed) == ([late.pk], [late.pk])


def test_an_interval_after_downtime_keeps_its_cadence(
    member: AbstractBaseUser, queue: RecordingQueue
) -> None:
    late = routine(member, utc(2026, 11, 16, 1), schedule=EVERY_6_HOURS)

    result = due_routines(TeamRoutine, sweep(queue), now=utc(2026, 11, 16, 20, 30))

    late.refresh_from_db()
    assert result.missed == [late.pk]
    assert late.next_fire_at == utc(2026, 11, 17, 1)


def test_a_routine_that_raises_is_left_for_the_next_sweep_and_the_rest_go_on(
    member: AbstractBaseUser, queue: RecordingQueue, caplog: pytest.LogCaptureFixture
) -> None:
    broken = routine(member, MONDAY_8 - timedelta(minutes=1), timezone="Nowhere/Atlantis")
    fine = routine(member, MONDAY_8)

    result = due_routines(TeamRoutine, sweep(queue), now=MONDAY_8)

    broken.refresh_from_db()
    assert (result.failed, result.fired) == ([broken.pk], [fine.pk])
    assert broken.next_fire_at == MONDAY_8 - timedelta(minutes=1)
    assert f"Routine {broken.pk} could not be swept" in caplog.text


def test_an_enqueue_that_raises_leaves_the_routine_as_it_was(
    member: AbstractBaseUser, queue: RecordingQueue
) -> None:
    due = routine(member, MONDAY_8)
    queue.refuse = True

    result = due_routines(TeamRoutine, sweep(queue), now=MONDAY_8)

    due.refresh_from_db()
    assert result.failed == [due.pk]
    assert (due.next_fire_at, due.last_run_id) == (MONDAY_8, None)


def test_a_sweep_claims_at_most_limit_routines_oldest_first(
    member: AbstractBaseUser, queue: RecordingQueue
) -> None:
    newer = routine(member, MONDAY_8)
    older = routine(member, MONDAY_8 - timedelta(hours=1))

    result = due_routines(TeamRoutine, sweep(queue), now=MONDAY_8, limit=1)

    assert result.fired == [older.pk]
    assert newer.pk not in result.fired


def test_a_routine_model_is_unswept_by_default() -> None:
    assert AbstractRoutine.routine_sweep() is None
