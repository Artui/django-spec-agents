from __future__ import annotations

from django_spec_agents.persistence.null_conversation_store import NullConversationStore
from django_spec_agents.persistence.preflight_identifiers import preflight_identifiers
from django_spec_agents.persistence.types.conversation_store import ConversationStore


class _Checking(NullConversationStore):
    def __init__(self) -> None:
        self.checked: list[dict[str, str | None]] = []

    def check_identifiers(self, **ids: str | None) -> None:
        self.checked.append(ids)


def test_a_store_with_the_check_is_asked_for_every_id() -> None:
    store = _Checking()

    preflight_identifiers(store, thread_id="t1", run_id=None)

    assert store.checked == [{"thread_id": "t1", "run_id": None}]


def test_a_store_without_the_check_is_accepted_and_left_alone() -> None:
    store = NullConversationStore()

    preflight_identifiers(store, thread_id="x" * 10_000)

    assert not hasattr(store, "check_identifiers")
    assert isinstance(store, ConversationStore)
    assert isinstance(_Checking(), ConversationStore)


def test_any_object_is_accepted() -> None:
    preflight_identifiers(object(), run_id="r1")
