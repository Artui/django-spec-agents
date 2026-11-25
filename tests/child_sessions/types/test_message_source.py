from __future__ import annotations

from django_spec_agents.child_sessions.types.message_source import MessageSource
from django_spec_agents.child_sessions.types.message_source_key import MESSAGE_SOURCE_KEY


def test_the_key_and_values_a_store_and_a_client_pin() -> None:
    assert MESSAGE_SOURCE_KEY == "django_spec_agents_source"
    assert [source.value for source in MessageSource] == ["wake", "task_output", "task_question"]
