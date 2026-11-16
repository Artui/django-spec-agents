from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.http import HttpRequest
from django.utils import timezone

from django_spec_agents.contrib.store.models import StoredRunEvent, StoredRunLog, StoredThreadRun
from django_spec_agents.contrib.store.utils import check_column_widths
from django_spec_agents.persistence.types.run_claim import RunClaim
from django_spec_agents.persistence.types.run_end_reason import RunEndReason
from django_spec_agents.persistence.types.run_log_entry import RunLogEntry
from django_spec_agents.persistence.types.run_outcome import RunOutcome
from django_spec_agents.persistence.types.run_waiting import RunWaiting
from django_spec_agents.persistence.types.thread_run_state import ThreadRunState
from django_spec_agents.persistence.types.thread_run_status import ThreadRunStatus
from django_spec_agents.persistence.utils import resolve_owner_id

_ENDINGS: dict[RunOutcome, tuple[ThreadRunStatus, RunEndReason | None]] = {
    RunOutcome.COMPLETED: (ThreadRunStatus.IDLE, None),
    RunOutcome.WAITING: (ThreadRunStatus.WAITING, None),
    RunOutcome.CANCELLED: (ThreadRunStatus.IDLE, None),
    RunOutcome.TIMED_OUT: (ThreadRunStatus.IDLE, RunEndReason.TIMED_OUT),
    RunOutcome.ERROR: (ThreadRunStatus.FAILED, RunEndReason.ERROR),
}


class DefaultRunStore:
    """A durable, owner-scoped [`RunStore`][django_spec_agents.RunStore] over the reference models.

    One instance serves every request, like
    [`DefaultConversationStore`][django_spec_agents.contrib.store.default_conversation_store.DefaultConversationStore]:
    each method resolves the owner from its request inside the
    ``sync_to_async`` hop, and refuses an anonymous request unless
    ``allow_anonymous`` buckets it by browser session.

    ``log_retention`` is how long a run's log outlives the run, a day by
    default; ``prune`` deletes the logs past it. A run that never finished
    (its owner died) counts from its last write, and a run under a live lease
    is never pruned. ``clock`` supplies "now" and exists for tests.
    """

    def __init__(
        self,
        *,
        allow_anonymous: bool = False,
        log_retention: timedelta = timedelta(days=1),
        clock: Callable[[], datetime] = timezone.now,
    ) -> None:
        self._allow_anonymous = allow_anonymous
        self._log_retention = log_retention
        self._clock = clock

    async def claim(
        self, thread_id: str, run_id: str, *, request: HttpRequest, lease: timedelta
    ) -> RunClaim:
        return await sync_to_async(self._claim)(thread_id, run_id, request, lease)

    async def renew(
        self, thread_id: str, run_id: str, *, request: HttpRequest, lease: timedelta
    ) -> bool:
        return await sync_to_async(self._renew)(thread_id, run_id, request, lease)

    async def finish(
        self,
        thread_id: str,
        run_id: str,
        outcome: RunOutcome,
        *,
        request: HttpRequest,
        waiting: RunWaiting | None = None,
    ) -> bool:
        if (outcome is RunOutcome.WAITING) != (waiting is not None):
            raise ValueError("waiting is given exactly when the outcome is WAITING")
        return await sync_to_async(self._finish)(thread_id, run_id, outcome, waiting, request)

    async def append(
        self, run_id: str, events: Sequence[Any], *, request: HttpRequest
    ) -> int | None:
        return await sync_to_async(self._append)(run_id, list(events), request)

    async def read(
        self, run_id: str, *, request: HttpRequest, after: int = 0, limit: int | None = None
    ) -> list[RunLogEntry]:
        return await sync_to_async(self._read)(run_id, after, limit, request)

    async def get(self, thread_id: str, *, request: HttpRequest) -> ThreadRunState | None:
        return (await self.get_many([thread_id], request=request)).get(thread_id)

    async def get_many(
        self, thread_ids: Sequence[str], *, request: HttpRequest
    ) -> dict[str, ThreadRunState]:
        return await sync_to_async(self._get_many)(list(thread_ids), request)

    async def request_cancel(self, thread_id: str, *, request: HttpRequest) -> bool:
        return await sync_to_async(self._request_cancel)(thread_id, request)

    async def mark_read(self, thread_id: str, *, request: HttpRequest) -> None:
        await sync_to_async(self._mark_read)(thread_id, request)

    async def prune(self) -> int:
        return await sync_to_async(self._prune)()

    def check_identifiers(self, *, thread_id: str | None = None, run_id: str | None = None) -> None:
        """Raise ``IdentifierTooLongError`` for an id its column cannot hold; writes nothing."""
        check_column_widths(StoredThreadRun, thread_id=thread_id, active_run_id=run_id)

    def _owner(self, request: HttpRequest) -> str:
        return resolve_owner_id(request, allow_anonymous=self._allow_anonymous)

    def _claim(
        self, thread_id: str, run_id: str, request: HttpRequest, lease: timedelta
    ) -> RunClaim:
        owner_id = self._owner(request)
        check_column_widths(StoredThreadRun, owner_id=owner_id)
        self.check_identifiers(thread_id=thread_id, run_id=run_id)
        now = self._clock()
        with transaction.atomic():
            StoredThreadRun.objects.get_or_create(owner_id=owner_id, thread_id=thread_id)
            rows = _thread(owner_id, thread_id)
            won = rows.filter(Q(active_run_id__isnull=True) | Q(lease_expires_at__lt=now)).update(
                status=ThreadRunStatus.RUNNING,
                reason=None,
                waiting=None,
                active_run_id=run_id,
                last_run_id=run_id,
                lease_expires_at=now + lease,
                cancel_requested_at=None,
                updated_at=now,
            )
            if not won:
                return RunClaim(
                    claimed=False, active_run_id=rows.values_list("active_run_id", flat=True).get()
                )
            StoredRunLog.objects.create(
                owner_id=owner_id,
                run_id=run_id,
                thread_id=thread_id,
                started_at=now,
                updated_at=now,
            )
        return RunClaim(claimed=True, active_run_id=run_id)

    def _renew(self, thread_id: str, run_id: str, request: HttpRequest, lease: timedelta) -> bool:
        owner_id = self._owner(request)
        now = self._clock()
        renewed = _held(owner_id, thread_id, run_id, now).update(
            lease_expires_at=now + lease, updated_at=now
        )
        if renewed:
            _log(owner_id, run_id).update(updated_at=now)
        return bool(renewed)

    def _finish(
        self,
        thread_id: str,
        run_id: str,
        outcome: RunOutcome,
        waiting: RunWaiting | None,
        request: HttpRequest,
    ) -> bool:
        owner_id = self._owner(request)
        now = self._clock()
        status, reason = _ENDINGS[outcome]
        with transaction.atomic():
            finished = _held(owner_id, thread_id, run_id, now).update(
                status=status,
                reason=reason,
                waiting=None if waiting is None else asdict(waiting),
                active_run_id=None,
                lease_expires_at=None,
                cancel_requested_at=None,
                updated_at=now,
            )
            if finished:
                _log(owner_id, run_id).update(ended_at=now, updated_at=now)
        return bool(finished)

    def _append(self, run_id: str, events: list[Any], request: HttpRequest) -> int | None:
        owner_id = self._owner(request)
        now = self._clock()
        with transaction.atomic():
            log = _log(owner_id, run_id).select_for_update().first()
            if log is None or not _held(owner_id, log.thread_id, run_id, now).exists():
                return None
            first = log.next_seq
            StoredRunEvent.objects.bulk_create(
                StoredRunEvent(log=log, seq=first + offset, event=event, created_at=now)
                for offset, event in enumerate(events)
            )
            _log(owner_id, run_id).update(next_seq=first + len(events), updated_at=now)
        return first + len(events) - 1

    def _read(
        self, run_id: str, after: int, limit: int | None, request: HttpRequest
    ) -> list[RunLogEntry]:
        rows = StoredRunEvent.objects.filter(
            log__owner_id=self._owner(request), log__run_id=run_id, seq__gt=after
        ).order_by("seq")
        if limit is not None:
            rows = rows[:limit]
        return [
            RunLogEntry(seq=row.seq, event=row.event, created_at=row.created_at) for row in rows
        ]

    def _get_many(self, thread_ids: list[str], request: HttpRequest) -> dict[str, ThreadRunState]:
        now = self._clock()
        rows = StoredThreadRun.objects.filter(
            owner_id=self._owner(request), thread_id__in=thread_ids
        )
        return {row.thread_id: _state(row, now) for row in rows}

    def _request_cancel(self, thread_id: str, request: HttpRequest) -> bool:
        now = self._clock()
        rows = _thread(self._owner(request), thread_id).filter(
            active_run_id__isnull=False, lease_expires_at__gte=now
        )
        return bool(rows.update(cancel_requested_at=now))

    def _mark_read(self, thread_id: str, request: HttpRequest) -> None:
        _thread(self._owner(request), thread_id).update(last_read_at=self._clock())

    def _prune(self) -> int:
        now = self._clock()
        cutoff = now - self._log_retention
        live = StoredThreadRun.objects.filter(
            owner_id=OuterRef("owner_id"),
            active_run_id=OuterRef("run_id"),
            lease_expires_at__gte=now,
        )
        # ``updated_at`` is the run's end, or its last write if it never ended.
        stale = StoredRunLog.objects.filter(updated_at__lt=cutoff).exclude(Exists(live))
        pks = list(stale.values_list("pk", flat=True))
        StoredRunLog.objects.filter(pk__in=pks).delete()
        return len(pks)


def _thread(owner_id: str, thread_id: str) -> QuerySet[StoredThreadRun]:
    return StoredThreadRun.objects.filter(owner_id=owner_id, thread_id=thread_id)


def _held(owner_id: str, thread_id: str, run_id: str, now: datetime) -> QuerySet[StoredThreadRun]:
    """The thread's row while ``run_id`` holds it under a live lease."""
    return _thread(owner_id, thread_id).filter(active_run_id=run_id, lease_expires_at__gte=now)


def _log(owner_id: str, run_id: str) -> QuerySet[StoredRunLog]:
    return StoredRunLog.objects.filter(owner_id=owner_id, run_id=run_id)


def _state(row: StoredThreadRun, now: datetime) -> ThreadRunState:
    status = ThreadRunStatus(row.status)
    reason = None if row.reason is None else RunEndReason(row.reason)
    active_run_id = row.active_run_id
    if row.lease_expires_at is not None and row.lease_expires_at < now:
        status, reason, active_run_id = ThreadRunStatus.FAILED, RunEndReason.INTERRUPTED, None
    return ThreadRunState(
        thread_id=row.thread_id,
        status=status,
        reason=reason,
        waiting=None if row.waiting is None else RunWaiting(**row.waiting),
        active_run_id=active_run_id,
        last_run_id=row.last_run_id,
        lease_expires_at=row.lease_expires_at,
        cancel_requested=row.cancel_requested_at is not None,
        last_read_at=row.last_read_at,
    )


__all__ = ["DefaultRunStore"]
