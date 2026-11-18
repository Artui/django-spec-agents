from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from django_spec_agents.child_sessions.model_inbound_queue import ModelInboundQueue
from django_spec_agents.child_sessions.types.inbound_kind import InboundKind
from django_spec_agents.child_sessions.types.inbound_message import InboundMessage
from django_spec_agents.contrib.store.models import StoredInboundMessage, StoredInboxClaim
from django_spec_agents.contrib.store.utils import check_column_widths
from django_spec_agents.persistence.types.thread_ref import ThreadRef


class DefaultInboundQueue(ModelInboundQueue):
    """A durable, owner-scoped inbound queue over ``StoredInboundMessage``.

    A thread's queue is its rows under ``(owner_id, scope, thread_id)``, an absent scope
    stored as NULL and kept apart from ``""``; ``ack`` deletes exactly the ids it is given.
    A run's claim on the queue is a ``StoredInboxClaim`` row. ``clock`` supplies "now" and
    exists for tests.
    """

    def __init__(
        self, *, allow_anonymous: bool = False, clock: Callable[[], datetime] = timezone.now
    ) -> None:
        super().__init__(allow_anonymous=allow_anonymous)
        self._clock = clock

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

    def _take(
        self, thread: ThreadRef, run_id: str, lease: timedelta, owner_id: str
    ) -> list[InboundMessage]:
        check_column_widths(
            StoredInboxClaim,
            owner_id=owner_id,
            scope=thread.scope,
            thread_id=thread.thread_id,
            run_id=run_id,
        )
        now = self._clock()
        with transaction.atomic():
            if not _claim(thread, run_id, owner_id, now, lease):
                return []
            rows = list(_queue(thread, owner_id).exclude(taken_by=run_id).order_by("id"))
            _queue(thread, owner_id).filter(id__in=[row.pk for row in rows]).update(taken_by=run_id)
        return [_message(row) for row in rows]

    def _release(self, thread: ThreadRef, run_id: str, owner_id: str) -> bool:
        with transaction.atomic():
            released, _ = _claims(thread, owner_id).filter(run_id=run_id).delete()
            _queue(thread, owner_id).filter(taken_by=run_id).update(taken_by=None)
        return bool(released)


def _queue(thread: ThreadRef, owner_id: str) -> QuerySet[StoredInboundMessage]:
    return StoredInboundMessage.objects.filter(
        owner_id=owner_id, scope=thread.scope, thread_id=thread.thread_id
    )


def _claims(thread: ThreadRef, owner_id: str) -> QuerySet[StoredInboxClaim]:
    return StoredInboxClaim.objects.filter(
        owner_id=owner_id, scope=thread.scope, thread_id=thread.thread_id
    )


def _claim(thread: ThreadRef, run_id: str, owner_id: str, now: datetime, lease: timedelta) -> bool:
    """Whether ``run_id`` now holds the inbox: its own claim, renewed, or one past its lease."""
    expires = now + lease
    StoredInboxClaim.objects.get_or_create(
        owner_id=owner_id,
        scope=thread.scope,
        thread_id=thread.thread_id,
        defaults={"run_id": run_id, "claimed_at": now, "lease_expires_at": expires},
    )
    claims = _claims(thread, owner_id)
    if claims.filter(run_id=run_id).update(lease_expires_at=expires):
        return True
    taken = claims.filter(lease_expires_at__lt=now)
    return bool(taken.update(run_id=run_id, claimed_at=now, lease_expires_at=expires))


def _message(row: StoredInboundMessage) -> InboundMessage:
    sender = row.sender_thread_id
    return InboundMessage(
        id=row.pk,
        kind=InboundKind(row.kind),
        body=row.body,
        created_at=row.created_at,
        sender=None if sender is None else ThreadRef(thread_id=sender, scope=row.sender_scope),
        taken_by=row.taken_by,
    )


__all__ = ["DefaultInboundQueue"]
