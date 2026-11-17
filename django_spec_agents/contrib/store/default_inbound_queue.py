from __future__ import annotations

from collections.abc import Collection
from typing import Any

from django.db.models import QuerySet

from django_spec_agents.child_sessions.model_inbound_queue import ModelInboundQueue
from django_spec_agents.child_sessions.types.inbound_kind import InboundKind
from django_spec_agents.child_sessions.types.inbound_message import InboundMessage
from django_spec_agents.contrib.store.models import StoredInboundMessage
from django_spec_agents.contrib.store.utils import check_column_widths
from django_spec_agents.persistence.types.thread_ref import ThreadRef


class DefaultInboundQueue(ModelInboundQueue):
    """A durable, owner-scoped inbound queue over ``StoredInboundMessage``.

    A thread's queue is its rows under ``(owner_id, scope, thread_id)``, an absent scope
    stored as NULL and kept apart from ``""``; ``ack`` deletes exactly the ids it is given.
    """

    def _enqueue(
        self,
        thread: ThreadRef,
        kind: InboundKind,
        body: Any,
        sender: ThreadRef | None,
        owner_id: str,
    ) -> int:
        row = StoredInboundMessage(
            owner_id=owner_id,
            scope=thread.scope,
            thread_id=thread.thread_id,
            kind=kind.value,
            body=body,
            sender_thread_id=None if sender is None else sender.thread_id,
            sender_scope=None if sender is None else sender.scope,
        )
        check_column_widths(
            StoredInboundMessage,
            owner_id=row.owner_id,
            scope=row.scope,
            thread_id=row.thread_id,
            sender_thread_id=row.sender_thread_id,
            sender_scope=row.sender_scope,
        )
        row.save()
        return row.pk

    def _pending(
        self, thread: ThreadRef, after: int, limit: int | None, owner_id: str
    ) -> list[InboundMessage]:
        rows = _queue(thread, owner_id).filter(id__gt=after).order_by("id")
        if limit is not None:
            rows = rows[:limit]
        return [_message(row) for row in rows]

    def _ack(self, thread: ThreadRef, ids: Collection[int], owner_id: str) -> int:
        removed, _ = _queue(thread, owner_id).filter(id__in=ids).delete()
        return removed


def _queue(thread: ThreadRef, owner_id: str) -> QuerySet[StoredInboundMessage]:
    return StoredInboundMessage.objects.filter(
        owner_id=owner_id, scope=thread.scope, thread_id=thread.thread_id
    )


def _message(row: StoredInboundMessage) -> InboundMessage:
    sender = row.sender_thread_id
    return InboundMessage(
        id=row.pk,
        kind=InboundKind(row.kind),
        body=row.body,
        created_at=row.created_at,
        sender=None if sender is None else ThreadRef(thread_id=sender, scope=row.sender_scope),
    )


__all__ = ["DefaultInboundQueue"]
