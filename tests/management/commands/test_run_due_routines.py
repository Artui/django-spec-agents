from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.core.management import call_command
from django.utils import timezone

from django_spec_agents.routines import RoutineSweep
from tests.routines.sweeping import EVERY_6_HOURS, RecordingQueue, routine, utc
from tests.routines.testapp.models import TeamRoutine


@pytest.fixture
def member(db: None) -> AbstractBaseUser:
    return get_user_model().objects.create_user(username="member")


def run(*args: str) -> str:
    out = StringIO()
    call_command("run_due_routines", *args, stdout=out)
    return out.getvalue()


def test_the_command_sweeps_each_model_that_names_a_queue(
    member: AbstractBaseUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = RecordingQueue()
    sweep = RoutineSweep(queue=queue, spec_name="run_routine")
    monkeypatch.setattr(TeamRoutine, "routine_sweep", classmethod(lambda cls: sweep))
    just_now = timezone.now() - timedelta(minutes=1)
    routine(member, just_now, schedule=EVERY_6_HOURS)
    routine(member, just_now, schedule=EVERY_6_HOURS)

    output = run("--limit", "1")

    assert len(queue.enqueued) == 1
    assert output == "routines_testapp.TeamRoutine: 1 fired, 0 missed, 0 busy, 0 paused, 0 failed\n"


def test_the_command_skips_a_model_with_no_sweep(member: AbstractBaseUser) -> None:
    due = routine(member, utc(2026, 1, 5, 8))

    assert run() == ""
    due.refresh_from_db()
    assert due.next_fire_at == utc(2026, 1, 5, 8)
