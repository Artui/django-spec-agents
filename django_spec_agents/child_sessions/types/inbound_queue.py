from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
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
    the ids it has handed on with ``ack``, so a message read by a process that then died
    is read again, and ``ack`` removes exactly those ids and no others. Ids are allocated
    at insert and seen at commit, so one can become visible behind a higher id already
    read: ``pending(after=n)`` used as a cursor can miss it. A reader that must see every
    message calls ``pending`` without ``after`` and acks what it read.

    **One run holds a thread's inbox at a time.** ``take`` claims it for a run, under a
    lease each call renews, and marks and returns the messages that run has not taken yet,
    so a run sees each message once. A run that holds no claim, because another run's lease
    is live, takes nothing. ``release`` drops the claim and hands the run's unacked messages
    on; a run that died without releasing is taken over once its lease lapses, and the next
    holder takes what the dead run took and never acked. ``pending`` lists every unacked
    message, taken or not.
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

    async def ack(self, thread: ThreadRef, ids: Iterable[int], *, request: HttpRequest) -> int:
        """Remove the thread's messages with these ``ids``; how many were removed."""
        ...

    async def take(
        self, thread: ThreadRef, run_id: str, *, request: HttpRequest, lease: timedelta
    ) -> list[InboundMessage]:
        """Claim or renew ``thread``'s inbox for ``run_id``; its messages not yet taken by it.

        Oldest first, each marked as taken by ``run_id``. ``[]`` while another run's claim is
        live.
        """
        ...

    async def release(self, thread: ThreadRef, run_id: str, *, request: HttpRequest) -> bool:
        """Drop ``run_id``'s claim and untake its unacked messages; whether it held the claim."""
        ...


__all__ = ["InboundQueue"]
