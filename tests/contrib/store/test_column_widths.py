from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from django.db.models import Model

from django_spec_agents.contrib.store.models import (
    StoredAttachment,
    StoredConversation,
    StoredMemory,
    StoredMemoryOperation,
    StoredRun,
    StoredSnapshot,
    StoredStepEvent,
    StoredToolEffect,
)
from tests.contrib.store.rows import too_long

# Collected by the PostgreSQL job too: SQLite stores any length, so only a real server can fail.
pytestmark = pytest.mark.django_db

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_EVENT = {"run_id": "r1", "kind": "run_started", "step_index": 0, "timestamp": _T0}
_EFFECT = {"run_id": "r1", "tool_call_id": "c1", "status": "started", "started_at": _T0}

_LIFTED: list[tuple[type[Model], str, dict[str, Any]]] = [
    (StoredAttachment, "name", {"attachment_id": "a1"}),
    (StoredAttachment, "mime", {"attachment_id": "a1"}),
    (StoredRun, "agent_name", {"run_id": "r1", "started_at": _T0}),
    (StoredStepEvent, "agent_name", _EVENT),
    (StoredStepEvent, "tool_name", _EVENT),
    (StoredSnapshot, "agent_name", {"run_id": "r1", "step_index": 0, "timestamp": _T0}),
    (StoredToolEffect, "tool_name", _EFFECT),
    (StoredToolEffect, "idempotency_key", {**_EFFECT, "tool_name": "search"}),
    (StoredMemoryOperation, "fingerprint", {"operation_id": "op-1"}),
]

_GUARDED = [
    (StoredConversation, "owner_id"),
    (StoredConversation, "thread_id"),
    (StoredAttachment, "owner_id"),
    (StoredRun, "run_id"),
    (StoredRun, "conversation_id"),
    (StoredRun, "parent_run_id"),
    (StoredStepEvent, "tool_call_id"),
    (StoredToolEffect, "tool_call_id"),
    (StoredMemory, "operation_id"),
    (StoredMemoryOperation, "operation_id"),
]


@pytest.mark.parametrize(
    ("model", "field", "required"),
    _LIFTED,
    ids=[f"{model.__name__}.{field}" for model, field, _ in _LIFTED],
)
def test_a_lifted_column_round_trips_a_300_character_value(
    model: type[Model], field: str, required: dict[str, Any]
) -> None:
    value = "x" * 300

    row = model.objects.create(**{**required, field: value})

    assert getattr(model.objects.get(pk=row.pk), field) == value


@pytest.mark.parametrize(
    ("model", "field"), _GUARDED, ids=[f"{model.__name__}.{field}" for model, field in _GUARDED]
)
def test_a_filter_on_an_over_long_id_finds_nothing_rather_than_failing(
    model: type[Model], field: str
) -> None:
    assert not model.objects.filter(**{field: too_long(model, field)}).exists()
