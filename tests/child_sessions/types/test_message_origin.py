from __future__ import annotations

from django_spec_agents.child_sessions.types.message_origin import MessageOrigin
from django_spec_agents.child_sessions.types.message_origin_key import MESSAGE_ORIGIN_KEY
from django_spec_agents.child_sessions.types.message_task_key import MESSAGE_TASK_KEY


def test_the_keys_and_values_a_store_and_a_client_pin() -> None:
    assert (MESSAGE_ORIGIN_KEY, MESSAGE_TASK_KEY) == ("origin", "task")
    assert [origin.value for origin in MessageOrigin] == ["wake", "task_output", "task_question"]
