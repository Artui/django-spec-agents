from __future__ import annotations

from django.http import HttpRequest

from django_spec_agents.contrib.store.default_conversation_store import DefaultConversationStore
from django_spec_agents.persistence.scoped_conversation_store import ScopedConversationStore
from django_spec_agents.persistence.types.conversation import Conversation
from django_spec_agents.persistence.types.conversation_meta import ConversationMetaList
from django_spec_agents.persistence.types.conversation_store import ConversationStore
from django_spec_agents.persistence.types.unlinkable_conversation_store import (
    UnlinkableConversationStore,
)


class _OlderConversationStore:
    async def load(self, thread_id: str, *, request: HttpRequest) -> Conversation | None: ...
    async def save(self, conversation: Conversation, *, request: HttpRequest) -> None: ...
    async def delete(self, thread_id: str, *, request: HttpRequest) -> None: ...
    async def list(
        self, *, request: HttpRequest, limit: int | None = None
    ) -> ConversationMetaList: ...
    async def exists(self, thread_id: str, *, request: HttpRequest) -> bool: ...
    async def rename(self, thread_id: str, title: str, *, request: HttpRequest) -> None: ...


def test_a_store_without_unlink_still_satisfies_the_required_protocol() -> None:
    store = _OlderConversationStore()

    assert isinstance(store, ConversationStore)
    assert not isinstance(store, UnlinkableConversationStore)
    assert not isinstance(ScopedConversationStore(store, scope="a"), UnlinkableConversationStore)


def test_the_default_store_and_a_wrapper_over_it_can_unlink() -> None:
    store = DefaultConversationStore()

    assert isinstance(store, UnlinkableConversationStore)
    assert isinstance(ScopedConversationStore(store, scope="a"), UnlinkableConversationStore)
