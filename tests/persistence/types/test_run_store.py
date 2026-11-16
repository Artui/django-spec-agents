from __future__ import annotations

from django.http import HttpRequest

from django_spec_agents.persistence.types.conversation import Conversation
from django_spec_agents.persistence.types.conversation_meta import ConversationMetaList
from django_spec_agents.persistence.types.conversation_store import ConversationStore
from django_spec_agents.persistence.types.run_store import RunStore


class _OlderConversationStore:
    async def load(self, thread_id: str, *, request: HttpRequest) -> Conversation | None: ...
    async def save(self, conversation: Conversation, *, request: HttpRequest) -> None: ...
    async def delete(self, thread_id: str, *, request: HttpRequest) -> None: ...
    async def list(
        self, *, request: HttpRequest, limit: int | None = None
    ) -> ConversationMetaList: ...
    async def exists(self, thread_id: str, *, request: HttpRequest) -> bool: ...
    async def rename(self, thread_id: str, title: str, *, request: HttpRequest) -> None: ...


def test_a_conversation_store_from_before_the_run_store_still_satisfies_its_protocol() -> None:
    store = _OlderConversationStore()

    assert isinstance(store, ConversationStore)
    assert not isinstance(store, RunStore)
