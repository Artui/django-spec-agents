from __future__ import annotations

from datetime import UTC, datetime

from django_spec_agents.persistence.types.conversation_meta import ConversationMeta
from django_spec_agents.persistence.types.thread_ref import ThreadRef


def test_defaults() -> None:
    meta = ConversationMeta(thread_id="t1", title="Hello")
    assert meta.thread_id == "t1"
    assert meta.title == "Hello"
    assert meta.updated_at is None
    assert meta.preview == ""
    assert meta.owner_id is None


def test_all_fields() -> None:
    when = datetime(2026, 6, 24, tzinfo=UTC)
    meta = ConversationMeta(
        thread_id="t1", title="Hi", updated_at=when, preview="how are you", owner_id="7"
    )
    assert meta.updated_at is when
    assert meta.preview == "how are you"
    assert meta.owner_id == "7"


def test_parent_defaults_to_none_so_an_older_store_constructs_it_unchanged() -> None:
    meta = ConversationMeta("t1", "Hi", None, "", "7")
    assert meta.parent is None


def test_parent_names_a_thread_in_another_scope() -> None:
    meta = ConversationMeta(thread_id="t2", title="Child", parent=ThreadRef("t1", scope="a"))
    assert meta.parent == ThreadRef(thread_id="t1", scope="a")
