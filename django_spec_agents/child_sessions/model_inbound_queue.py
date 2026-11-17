from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from asgiref.sync import sync_to_async
from django.http import HttpRequest

from django_spec_agents.child_sessions.types.inbound_kind import InboundKind
from django_spec_agents.child_sessions.types.inbound_message import InboundMessage
from django_spec_agents.persistence.types.thread_ref import ThreadRef
from django_spec_agents.persistence.utils import resolve_owner_id


class ModelInboundQueue(ABC):
    """Abstract base for a model-backed (or any sync) inbound queue.

    Provides the async wrapping and per-request owner scoping, as
    [`ModelConversationStore`][django_spec_agents.ModelConversationStore] does; a
    subclass implements ``_enqueue``, ``_pending`` and ``_ack`` against its own
    model, and **must** filter each by the resolved ``owner_id``. ``allow_anonymous``
    is off by default: a headless run's owner is always a user.
    """

    def __init__(self, *, allow_anonymous: bool = False) -> None:
        self._allow_anonymous = allow_anonymous

    async def enqueue(
        self,
        thread: ThreadRef,
        kind: InboundKind,
        body: Any,
        *,
        request: HttpRequest,
        sender: ThreadRef | None = None,
    ) -> int:
        return await sync_to_async(self._enqueue_scoped)(thread, kind, body, sender, request)

    async def pending(
        self, thread: ThreadRef, *, request: HttpRequest, after: int = 0, limit: int | None = None
    ) -> list[InboundMessage]:
        return await sync_to_async(self._pending_scoped)(thread, after, limit, request)

    async def ack(self, thread: ThreadRef, through: int, *, request: HttpRequest) -> int:
        return await sync_to_async(self._ack_scoped)(thread, through, request)

    def _owner(self, request: HttpRequest) -> str:
        return resolve_owner_id(request, allow_anonymous=self._allow_anonymous)

    def _enqueue_scoped(
        self,
        thread: ThreadRef,
        kind: InboundKind,
        body: Any,
        sender: ThreadRef | None,
        request: HttpRequest,
    ) -> int:
        return self._enqueue(thread, kind, body, sender, self._owner(request))

    def _pending_scoped(
        self, thread: ThreadRef, after: int, limit: int | None, request: HttpRequest
    ) -> list[InboundMessage]:
        return self._pending(thread, after, limit, self._owner(request))

    def _ack_scoped(self, thread: ThreadRef, through: int, request: HttpRequest) -> int:
        return self._ack(thread, through, self._owner(request))

    @abstractmethod
    def _enqueue(
        self,
        thread: ThreadRef,
        kind: InboundKind,
        body: Any,
        sender: ThreadRef | None,
        owner_id: str,
    ) -> int: ...

    @abstractmethod
    def _pending(
        self, thread: ThreadRef, after: int, limit: int | None, owner_id: str
    ) -> list[InboundMessage]: ...

    @abstractmethod
    def _ack(self, thread: ThreadRef, through: int, owner_id: str) -> int: ...


__all__ = ["ModelInboundQueue"]
