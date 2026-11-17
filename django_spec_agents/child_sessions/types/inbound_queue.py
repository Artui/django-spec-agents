from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from django.http import HttpRequest

from django_spec_agents.child_sessions.types.inbound_kind import InboundKind
from django_spec_agents.child_sessions.types.inbound_message import InboundMessage
from django_spec_agents.persistence.types.thread_ref import ThreadRef


@runtime_checkable
class InboundQueue(Protocol):
    """A durable queue of messages per thread: reports, questions, answers and steering.

    Its own protocol, so a [`ConversationStore`][django_spec_agents.ConversationStore]
    gains no method. Keyed like one, by the owner each method resolves from its request,
    and addressed by
    [`ThreadRef`][django_spec_agents.persistence.types.thread_ref.ThreadRef],
    so a child in one scope reaches its parent in another.

    **Delivery is at least once.** ``pending`` reads without removing; a reader acks
    what it has handed on with ``ack``, so a message read by a process that then died
    is read again. Ids grow in the order messages were enqueued.
    """

    async def enqueue(
        self,
        thread: ThreadRef,
        kind: InboundKind,
        body: Any,
        *,
        request: HttpRequest,
        sender: ThreadRef | None = None,
    ) -> int:
        """Append a message to ``thread``'s queue and return its id."""
        ...

    async def pending(
        self, thread: ThreadRef, *, request: HttpRequest, after: int = 0, limit: int | None = None
    ) -> list[InboundMessage]:
        """The thread's unacknowledged messages with ids above ``after``, oldest first."""
        ...

    async def ack(self, thread: ThreadRef, through: int, *, request: HttpRequest) -> int:
        """Remove the thread's messages with ids up to ``through``; how many were removed."""
        ...


__all__ = ["InboundQueue"]
