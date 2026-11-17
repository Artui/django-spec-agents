from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from django.contrib.auth.base_user import AbstractBaseUser
from django.db import transaction

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
    assert due.last_thread_id
    params = {"routine_id": due.pk, "thread_id": due.last_thread_id}
    assert queue.enqueued == [Enqueued(FIRE, params, member)]
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
    assert (lapsed.enabled, lapsed.next_fire_at, lapsed.last_run_id) == (False, None, None)
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


def test_a_routine_paused_for_a_lapse_fires_again_once_turned_back_on(
    member: AbstractBaseUser, queue: RecordingQueue
) -> None:
    lapsed = routine(member, MONDAY_8, last_run_id="run-7")
    queue.states["run-7"] = FiringState.LAPSED
    due_routines(TeamRoutine, sweep(queue), now=MONDAY_8)
    lapsed.refresh_from_db()
    lapsed.enabled, lapsed.next_fire_at, lapsed.paused_reason = True, TUESDAY_8, ""
    lapsed.save()

    result = due_routines(TeamRoutine, sweep(queue), now=TUESDAY_8)

    assert result.fired == [lapsed.pk]


def test_each_firing_runs_into_a_thread_of_its_own(
    member: AbstractBaseUser, queue: RecordingQueue
) -> None:
    routine(member, MONDAY_8)
    due_routines(TeamRoutine, sweep(queue), now=MONDAY_8)
    due_routines(TeamRoutine, sweep(queue), now=TUESDAY_8)

    first, second = (entry.params["thread_id"] for entry in queue.enqueued)
    assert first != second


@pytest.mark.parametrize("state", [FiringState.FINISHED, None])
def test_a_firing_whose_thread_still_waits_skips_this_one(
    member: AbstractBaseUser,
    queue: RecordingQueue,
    monkeypatch: pytest.MonkeyPatch,
    state: FiringState | None,
) -> None:
    asked: list[tuple[str, AbstractBaseUser]] = []

    def thread_waits(cls: type[TeamRoutine], thread_id: str, principal: AbstractBaseUser) -> bool:
        asked.append((thread_id, principal))
        return True

    monkeypatch.setattr(TeamRoutine, "thread_waits", classmethod(thread_waits))
    waiting = routine(member, MONDAY_8, last_run_id="run-7", last_thread_id="thread-7")
    if state is not None:
        queue.states["run-7"] = state

    result = due_routines(TeamRoutine, sweep(queue), now=MONDAY_8)

    waiting.refresh_from_db()
    assert queue.enqueued == []
    assert result.busy == [waiting.pk]
    assert asked == [("thread-7", member)]
    assert (waiting.next_fire_at, waiting.last_thread_id) == (TUESDAY_8, "thread-7")


def test_a_thread_the_host_says_is_done_lets_the_next_firing_run(
    member: AbstractBaseUser, queue: RecordingQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(TeamRoutine, "thread_waits", classmethod(lambda cls, thread, user: False))
    done = routine(member, MONDAY_8, last_run_id="run-7", last_thread_id="thread-7")
    queue.states["run-7"] = FiringState.FINISHED

    result = due_routines(TeamRoutine, sweep(queue), now=MONDAY_8)

    assert result.fired == [done.pk]


def test_no_thread_waits_by_default(member: AbstractBaseUser) -> None:
    assert AbstractRoutine.thread_waits("thread-7", member) is False


def test_a_routine_that_never_fired_asks_about_no_thread(
    member: AbstractBaseUser, queue: RecordingQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(TeamRoutine, "thread_waits", classmethod(lambda cls, thread, user: True))
    fresh = routine(member, MONDAY_8)

    result = due_routines(TeamRoutine, sweep(queue), now=MONDAY_8)

    assert result.fired == [fresh.pk]


@dataclass
class OnCommitQueue(RecordingQueue):
    sent: list[str] = field(default_factory=list)

    def enqueue(self, spec_name: str, params: Mapping[str, Any], *, actor: Any) -> str:
        run_id = super().enqueue(spec_name, params, actor=actor)
        transaction.on_commit(lambda: self.sent.append(params["thread_id"]))
        return run_id


@pytest.mark.django_db(transaction=True)
def test_a_routine_that_raises_after_its_enqueue_sends_nothing(
    member: AbstractBaseUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken, fine = routine(member, MONDAY_8), routine(member, MONDAY_8)
    save = TeamRoutine.save

    def failing_save(self: TeamRoutine, *args: Any, **kwargs: Any) -> None:
        if self.pk == broken.pk:
            raise RuntimeError("the row would not save")
        save(self, *args, **kwargs)

    monkeypatch.setattr(TeamRoutine, "save", failing_save)
    queue = OnCommitQueue()

    result = due_routines(TeamRoutine, sweep(queue), now=MONDAY_8)

    fine.refresh_from_db()
    assert (result.failed, result.fired) == ([broken.pk], [fine.pk])
    assert queue.sent == [fine.last_thread_id]
