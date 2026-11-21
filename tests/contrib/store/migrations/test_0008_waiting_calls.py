from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

LABEL = "django_spec_agents_store"
BEFORE = "0007_thread_run_prior"
AFTER = "0008_waiting_calls"
_CALL = {"kind": "question", "summary": "ask_parent", "tool_call_id": "q1", "destructive": False}
_OTHER = {"kind": "approval", "summary": "drop", "tool_call_id": "a1", "destructive": True}

pytestmark = pytest.mark.django_db(transaction=True)


def _migrate(target: str) -> None:
    MigrationExecutor(connection).migrate([(LABEL, target)])


def _model(at: str) -> Any:
    state = MigrationExecutor(connection).loader.project_state((LABEL, at))
    return state.apps.get_model(LABEL, "StoredThreadRun")


def _rows(at: str) -> dict[str, tuple[Any, Any]]:
    rows = _model(at).objects.values_list("thread_id", "waiting", "prior_waiting")
    return {thread_id: (waiting, prior) for thread_id, waiting, prior in rows}


@pytest.fixture
def migrated_forward() -> Iterator[None]:
    try:
        yield
    finally:
        _migrate(MigrationExecutor(connection).loader.graph.leaf_nodes(LABEL)[0][1])


@pytest.mark.usefixtures("migrated_forward")
def test_a_stored_single_call_becomes_a_one_element_list() -> None:
    _migrate(BEFORE)
    run = _model(BEFORE).objects
    run.create(thread_id="parked", status="waiting", waiting=_CALL)
    run.create(thread_id="resumed", status="running", prior_status="waiting", prior_waiting=_CALL)
    run.create(thread_id="idle", status="idle")

    _migrate(AFTER)

    assert _rows(AFTER) == {
        "parked": ([_CALL], None),
        "resumed": (None, [_CALL]),
        "idle": (None, None),
    }


@pytest.mark.usefixtures("migrated_forward")
def test_unapplying_keeps_the_first_call_of_each_list() -> None:
    _migrate(AFTER)
    run = _model(AFTER).objects
    run.create(thread_id="parked", status="waiting", waiting=[_CALL, _OTHER])
    run.create(thread_id="resumed", status="running", prior_waiting=[_OTHER])
    run.create(thread_id="idle", status="idle")

    _migrate(BEFORE)

    assert _rows(BEFORE) == {
        "parked": (_CALL, None),
        "resumed": (None, _OTHER),
        "idle": (None, None),
    }
