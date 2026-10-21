from __future__ import annotations

import pytest

from django_spec_agents.contrib.store.models import (
    ConversationAttachment,
    StoredAttachment,
    StoredConversation,
    StoredMemory,
    StoredMemoryOperation,
    StoredRun,
    StoredSnapshot,
    StoredStepEvent,
    StoredToolEffect,
)


@pytest.mark.parametrize(
    ("instance", "expected"),
    [
        (StoredConversation(thread_id="t1", title="Trip planning"), "Trip planning"),
        (StoredConversation(thread_id="t1", title=""), "t1"),
        (StoredAttachment(attachment_id="a1", name="notes.txt"), "notes.txt"),
        (StoredAttachment(attachment_id="a1", name=""), "a1"),
        (
            ConversationAttachment(
                conversation=StoredConversation(thread_id="t1"),
                attachment=StoredAttachment(attachment_id="a1"),
            ),
            "t1:a1",
        ),
        (StoredRun(run_id="run-1"), "run-1"),
        (StoredStepEvent(run_id="run-1", kind="run_started"), "run-1:run_started"),
        (StoredSnapshot(run_id="run-1", step_index=3), "run-1@3"),
        (StoredToolEffect(run_id="run-1", tool_call_id="c1", status="started"), "run-1:c1:started"),
        (StoredMemory(path="u-7/main/MEMORY.md"), "u-7/main/MEMORY.md"),
        (StoredMemoryOperation(operation_id="op-1"), "op-1"),
    ],
    ids=[
        "conversation-title",
        "conversation-thread-id-fallback",
        "attachment-name",
        "attachment-id-fallback",
        "conversation-attachment-link",
        "run-id",
        "step-event-run-and-kind",
        "snapshot-run-and-step",
        "tool-effect-run-call-status",
        "memory-path",
        "memory-operation-id",
    ],
)
def test_str_names_the_row(instance: object, expected: str) -> None:
    assert str(instance) == expected
