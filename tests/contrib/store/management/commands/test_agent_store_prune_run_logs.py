from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from django_spec_agents.contrib.store.models import StoredRunEvent, StoredRunLog, StoredThreadRun

pytestmark = pytest.mark.django_db

COMMAND = "agent_store_prune_run_logs"


def _log(run_id: str, *, age: timedelta, live: bool = False) -> StoredRunLog:
    now = timezone.now()
    then = now - age
    log = StoredRunLog.objects.create(
        owner_id="7",
        run_id=run_id,
        thread_id=f"t-{run_id}",
        next_seq=2,
        started_at=then,
        updated_at=then,
        ended_at=None if live else then,
    )
    StoredRunEvent.objects.create(log=log, seq=1, event={"n": 1}, created_at=then)
    if live:
        StoredThreadRun.objects.create(
            owner_id="7",
            thread_id=log.thread_id,
            status="running",
            active_run_id=run_id,
            last_run_id=run_id,
            lease_expires_at=now + timedelta(minutes=5),
        )
    return log


def _run(*args: str) -> str:
    out = StringIO()
    call_command(COMMAND, *args, stdout=out)
    return out.getvalue()


def _kept() -> set[str]:
    return set(StoredRunLog.objects.values_list("run_id", flat=True))


def test_deletes_a_log_past_retention_and_keeps_a_live_run_s() -> None:
    _log("old", age=timedelta(days=2))
    _log("live", age=timedelta(days=2), live=True)
    _log("recent", age=timedelta(hours=1))

    assert _run() == "Deleted 1 run log(s).\n"

    assert _kept() == {"live", "recent"}
    assert StoredRunEvent.objects.count() == 2


def test_the_retention_takes_a_unit() -> None:
    _log("old", age=timedelta(hours=2))

    _run("--older-than", "3h")
    assert _kept() == {"old"}
    _run("--older-than", "90m")
    assert _kept() == set()


def test_a_dry_run_reports_and_deletes_nothing() -> None:
    _log("old", age=timedelta(days=2))
    _log("live", age=timedelta(days=2), live=True)

    assert _run("--dry-run") == "Would delete 1 run log(s).\n"

    assert _kept() == {"old", "live"}


def test_an_unreadable_retention_is_refused() -> None:
    with pytest.raises(CommandError, match="Could not read --older-than"):
        _run("--older-than", "soon")
