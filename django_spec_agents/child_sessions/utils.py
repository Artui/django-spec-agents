from __future__ import annotations

from typing import Any

from django_spec_agents.child_sessions.types.message_origin import MessageOrigin
from django_spec_agents.child_sessions.types.message_origin_key import MESSAGE_ORIGIN_KEY
from django_spec_agents.child_sessions.types.message_task_key import MESSAGE_TASK_KEY
from django_spec_agents.persistence.types.thread_ref import ThreadRef


def task_metadata(
    origin: MessageOrigin,
    child: ThreadRef | None,
    *,
    agent_name: str | None,
    body: str | None,
    outcome: str | None = None,
    question_id: str | None = None,
) -> dict[str, Any]:
    """The metadata of a report or question part, leaving out each field that is not known."""
    thread_id = None if child is None else child.thread_id
    fields = {
        "taskId": thread_id,
        "threadId": thread_id,
        "agentName": agent_name,
        "outcome": outcome,
        "questionId": question_id,
        "body": body,
    }
    known = {key: value for key, value in fields.items() if value is not None}
    return {MESSAGE_ORIGIN_KEY: origin.value, MESSAGE_TASK_KEY: known}
