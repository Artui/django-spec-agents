from __future__ import annotations

from abc import ABC, abstractmethod

from asgiref.sync import sync_to_async
from django.http import HttpRequest

from django_spec_agents.persistence.types.conversation import Conversation
from django_spec_agents.persistence.types.conversation_meta import ConversationMetaList
from django_spec_agents.persistence.utils import resolve_owner_id


class ModelConversationStore(ABC):
    """Abstract base for a model-backed (or any sync) ``ConversationStore``."""

    # A class-level default, so a subclass that skips super() refuses anonymous requests.
    _allow_anonymous: bool = False

    def __init__(self, *, allow_anonymous: bool = False) -> None:
        """A subclass overriding this must call ``super().__init__()``."""
        self._allow_anonymous: bool = allow_anonymous

    async def load(self, thread_id: str, *, request: HttpRequest) -> Conversation | None:
        return await sync_to_async(self._load)(thread_id, request)

    async def save(self, conversation: Conversation, *, request: HttpRequest) -> None:
        await sync_to_async(self._save)(conversation, request)

    async def delete(self, thread_id: str, *, request: HttpRequest) -> None:
        await sync_to_async(self._delete)(thread_id, request)

    async def list(self, *, request: HttpRequest, limit: int | None = None) -> ConversationMetaList:
        return await sync_to_async(self._list_scoped)(request, limit)

    async def exists(self, thread_id: str, *, request: HttpRequest) -> bool:
        return await sync_to_async(self._exists_scoped)(thread_id, request)

    async def rename(self, thread_id: str, title: str, *, request: HttpRequest) -> None:
        await sync_to_async(self._rename_scoped)(thread_id, title, request)

    # resolve_owner_id may create a session row, so this must not run on the event loop.
    def _load(self, thread_id: str, request: HttpRequest) -> Conversation | None:
        return self._fetch(
            thread_id, resolve_owner_id(request, allow_anonymous=self._allow_anonymous)
        )

    def _save(self, conversation: Conversation, request: HttpRequest) -> None:
        self._store(conversation, resolve_owner_id(request, allow_anonymous=self._allow_anonymous))

    def _delete(self, thread_id: str, request: HttpRequest) -> None:
        self._remove(thread_id, resolve_owner_id(request, allow_anonymous=self._allow_anonymous))

    def _list_scoped(self, request: HttpRequest, limit: int | None) -> ConversationMetaList:
        return self._list(resolve_owner_id(request, allow_anonymous=self._allow_anonymous), limit)

    def _exists_scoped(self, thread_id: str, request: HttpRequest) -> bool:
        return self._exists(
            thread_id, resolve_owner_id(request, allow_anonymous=self._allow_anonymous)
        )

    def _rename_scoped(self, thread_id: str, title: str, request: HttpRequest) -> None:
        self._rename(
            thread_id, title, resolve_owner_id(request, allow_anonymous=self._allow_anonymous)
        )

    @abstractmethod
    def _fetch(self, thread_id: str, owner_id: str | None) -> Conversation | None: ...

    @abstractmethod
    def _store(self, conversation: Conversation, owner_id: str | None) -> None: ...

    @abstractmethod
    def _remove(self, thread_id: str, owner_id: str | None) -> None: ...

    def _list(self, owner_id: str | None, limit: int | None) -> ConversationMetaList:
        """Owner-scoped thread metadata for the drawer."""
        return []

    def _exists(self, thread_id: str, owner_id: str | None) -> bool:
        """Owner-scoped presence check, probing via ``_fetch`` by default."""
        return self._fetch(thread_id, owner_id) is not None

    def _rename(self, thread_id: str, title: str, owner_id: str | None) -> None:
        """Persist a renamed display title."""
        return None


__all__ = ["ModelConversationStore"]
