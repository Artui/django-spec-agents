from __future__ import annotations

from typing import Any

from django_spec_agents.child_sessions.types.inbound_queue import InboundQueue
from django_spec_agents.contrib.store.default_conversation_store import DefaultConversationStore
from django_spec_agents.contrib.store.default_inbound_queue import DefaultInboundQueue
from django_spec_agents.persistence.types.conversation_store import ConversationStore


class _OlderStore:
    """A third-party store written against the protocol before the queue existed."""

    async def load(self, thread_id: str, *, request: Any) -> Any: ...
    async def save(self, conversation: Any, *, request: Any) -> None: ...
    async def delete(self, thread_id: str, *, request: Any) -> None: ...
    async def list(self, *, request: Any, limit: int | None = None) -> list[Any]: ...
    async def exists(self, thread_id: str, *, request: Any) -> bool: ...
    async def rename(self, thread_id: str, title: str, *, request: Any) -> None: ...


def test_an_older_store_still_satisfies_the_conversation_store() -> None:
    assert isinstance(_OlderStore(), ConversationStore)
    assert not isinstance(_OlderStore(), InboundQueue)


def test_the_conversation_store_protocol_gained_no_queue_method() -> None:
    assert not {"enqueue", "pending", "ack", "take", "release"} & set(dir(ConversationStore))
    assert not isinstance(DefaultConversationStore(), InboundQueue)


def test_the_reference_queue_satisfies_the_protocol() -> None:
    assert isinstance(DefaultInboundQueue(), InboundQueue)


class _QueueWithoutClaims:
    async def enqueue(self, thread: Any, kind: Any, body: Any, *, request: Any) -> int: ...
    async def pending(self, thread: Any, *, request: Any) -> list[Any]: ...
    async def ack(self, thread: Any, ids: Any, *, request: Any) -> int: ...


def test_a_queue_that_cannot_take_or_release_is_not_an_inbound_queue() -> None:
    assert not isinstance(_QueueWithoutClaims(), InboundQueue)
