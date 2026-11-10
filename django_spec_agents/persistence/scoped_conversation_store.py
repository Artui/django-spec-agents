from __future__ import annotations

import dataclasses

from django.http import HttpRequest

from django_spec_agents.persistence.types.conversation import Conversation
from django_spec_agents.persistence.types.conversation_meta import ConversationMetaList
from django_spec_agents.persistence.types.conversation_store import ConversationStore


class ScopedConversationStore:
    """Partition another ``ConversationStore`` by a scope name."""

    def __init__(self, inner: ConversationStore, *, scope: str) -> None:
        if ":" in scope:
            raise ValueError(
                f"scope {scope!r} contains ':', which separates the scope from the "
                "thread id in the storage key. A scope holding one can collide with "
                "another scope's threads; use a different separator."
            )
        self._inner = inner
        self._scope = scope

    def _key(self, thread_id: str) -> str:
        return f"{self._scope}:{thread_id}"

    def _unkey(self, thread_id: str) -> str:
        prefix = f"{self._scope}:"
        return thread_id[len(prefix) :] if thread_id.startswith(prefix) else thread_id

    async def load(self, thread_id: str, *, request: HttpRequest) -> Conversation | None:
        conversation = await self._inner.load(self._key(thread_id), request=request)
        if conversation is None:
            return None
        # Hand back the id the client asked for, not the storage key.
        return dataclasses.replace(conversation, thread_id=self._unkey(conversation.thread_id))

    async def save(self, conversation: Conversation, *, request: HttpRequest) -> None:
        await self._inner.save(
            dataclasses.replace(conversation, thread_id=self._key(conversation.thread_id)),
            request=request,
        )

    async def delete(self, thread_id: str, *, request: HttpRequest) -> None:
        await self._inner.delete(self._key(thread_id), request=request)

    async def list(self, *, request: HttpRequest, limit: int | None = None) -> ConversationMetaList:
        """This scope's threads only, with storage keys translated back."""
        prefix = f"{self._scope}:"
        return [
            dataclasses.replace(meta, thread_id=self._unkey(meta.thread_id))
            for meta in await self._inner.list(request=request, limit=limit)
            if meta.thread_id.startswith(prefix)
        ]

    async def exists(self, thread_id: str, *, request: HttpRequest) -> bool:
        return await self._inner.exists(self._key(thread_id), request=request)

    async def rename(self, thread_id: str, title: str, *, request: HttpRequest) -> None:
        await self._inner.rename(self._key(thread_id), title, request=request)


__all__ = ["ScopedConversationStore"]
