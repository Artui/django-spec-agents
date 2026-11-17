from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.db import connection

from django_spec_agents.routines import RoutineSweep, SweepResult, due_routines
from tests.routines.sweeping import RecordingQueue, routine, utc
from tests.routines.testapp.models import TeamRoutine

MONDAY_8 = utc(2026, 11, 16, 8)

pytestmark = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="SQLite ignores select_for_update"
)


@dataclass
class HoldingQueue(RecordingQueue):
    """Holds the first sweep's transaction open, its row locked, until the second sweep ends."""

    claimed: threading.Event = field(default_factory=threading.Event)
    second_done: threading.Event = field(default_factory=threading.Event)
    held: list[bool] = field(default_factory=list)

    def enqueue(self, spec_name: str, params: Mapping[str, Any], *, actor: Any) -> str:
        run_id = super().enqueue(spec_name, params, actor=actor)
        self.claimed.set()
        self.held.append(self.second_done.wait(timeout=5))
        return run_id


@pytest.mark.django_db(transaction=True)
def test_two_sweeps_at_once_fire_a_due_routine_once() -> None:
    member = get_user_model().objects.create_user(username="member")
    due = routine(member, MONDAY_8)
    queue = HoldingQueue()
    sweep = RoutineSweep(queue=queue, spec_name="run_routine")
    first: list[SweepResult] = []

    def sweep_in_another_connection() -> None:
        first.append(due_routines(TeamRoutine, sweep, now=MONDAY_8))
        connection.close()

    worker = threading.Thread(target=sweep_in_another_connection)
    worker.start()
    assert queue.claimed.wait(timeout=5)
    second = due_routines(TeamRoutine, sweep, now=MONDAY_8)
    queue.second_done.set()
    worker.join()

    assert queue.held == [True]
    assert (first[0].fired, second.fired) == ([due.pk], [])
    assert len(queue.enqueued) == 1
