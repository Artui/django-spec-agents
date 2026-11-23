from __future__ import annotations

from asyncio import sleep
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta
from time import monotonic
from typing import Any, NoReturn, cast

from asgiref.sync import sync_to_async
from django.db import connections, router, transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import Case, CharField, Exists, F, OuterRef, Q, QuerySet, Value, When
from django.db.models.sql import UpdateQuery
from django.http import HttpRequest
from django.utils import timezone

from django_spec_agents.contrib.store.models import StoredRunEvent, StoredRunLog, StoredThreadRun
from django_spec_agents.contrib.store.utils import check_column_widths, stale_run_logs
from django_spec_agents.persistence.run_id_reused_error import RunIdReusedError
from django_spec_agents.persistence.run_not_parked_error import RunNotParkedError
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
    """A durable, owner-scoped run store over the reference models.

    It is a [`ResumableRunStore`][django_spec_agents.ResumableRunStore] and a
    [`WatchableRunStore`][django_spec_agents.WatchableRunStore].

    One instance serves every request, like
    [`DefaultConversationStore`][django_spec_agents.contrib.store.default_conversation_store.DefaultConversationStore]:
    each method resolves the owner from its request inside the
    ``sync_to_async`` hop, and refuses an anonymous request unless
    ``allow_anonymous`` buckets it by browser session.

    ``log_retention`` is how long a run's log outlives the run, a day by
    default; ``prune`` deletes the logs past it. A run that never finished
    (its owner died) counts from its last write, and a run under a live lease
    is never pruned; the host schedules ``agent_store_prune_run_logs`` to
    run it.

    ``wait`` polls the log with one indexed query per tick, pausing
    ``poll_interval[0]`` seconds after the first and doubling up to
    ``poll_interval[1]`` while nothing changes; every call starts from the
    shortest pause. ``clock`` supplies "now" and exists for tests.
    """

    def __init__(
        self,
        *,
        allow_anonymous: bool = False,
        log_retention: timedelta = timedelta(days=1),
        poll_interval: tuple[float, float] = (0.5, 2.0),
        clock: Callable[[], datetime] = timezone.now,
    ) -> None:
        if not 0 < poll_interval[0] <= poll_interval[1]:
            raise ValueError(f"poll_interval {poll_interval!r} must be 0 < first <= largest")
        self._allow_anonymous = allow_anonymous
        self._log_retention = log_retention
        self._poll_interval = poll_interval
        self._clock = clock

    async def claim(
        self,
        thread_id: str,
        run_id: str,
        *,
        request: HttpRequest,
        lease: timedelta,
        resumes: str | None = None,
    ) -> RunClaim:
        return await sync_to_async(self._claim)(thread_id, run_id, request, lease, resumes)

    async def release(self, thread_id: str, run_id: str, *, request: HttpRequest) -> bool:
        return await sync_to_async(self._release)(thread_id, run_id, request)

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
        waiting: tuple[RunWaiting, ...] = (),
    ) -> bool:
        if (outcome is RunOutcome.WAITING) != bool(waiting):
            raise ValueError("waiting is non-empty exactly when the outcome is WAITING")
        return await sync_to_async(self._finish)(thread_id, run_id, outcome, waiting, request)

    async def append(
        self, run_id: str, events: Sequence[Any], *, request: HttpRequest
    ) -> int | None:
        return await sync_to_async(self._append)(run_id, list(events), request)

    async def read(
        self, run_id: str, *, request: HttpRequest, after: int = 0, limit: int | None = None
    ) -> list[RunLogEntry]:
        return await sync_to_async(self._read)(run_id, after, limit, request)

    async def wait(
        self, run_id: str, *, after: int, timeout: timedelta, request: HttpRequest
    ) -> bool:
        deadline = monotonic() + timeout.total_seconds()
        pause, longest = self._poll_interval
        while not await sync_to_async(self._moved)(run_id, after, request):
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            await sleep(min(pause, remaining))
            pause = min(pause * 2, longest)
        return True

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
        self,
        thread_id: str,
        run_id: str,
        request: HttpRequest,
        lease: timedelta,
        resumes: str | None,
    ) -> RunClaim:
        owner_id = self._owner(request)
        check_column_widths(StoredThreadRun, owner_id=owner_id)
        self.check_identifiers(thread_id=thread_id, run_id=run_id)
        now = self._clock()
        with transaction.atomic():
            if _log(owner_id, run_id).exists():
                raise RunIdReusedError(f"run id {run_id!r} was claimed before", run_id=run_id)
            if resumes is None:
                StoredThreadRun.objects.get_or_create(owner_id=owner_id, thread_id=thread_id)
                free = Q(active_run_id__isnull=True) | Q(lease_expires_at__lt=now)
            else:
                free = Q(
                    active_run_id__isnull=True,
                    status=ThreadRunStatus.WAITING,
                    last_run_id=resumes,
                )
            rows = _thread(owner_id, thread_id)
            # The prior state goes first: MySQL evaluates SET left to right on the new values.
            won = rows.filter(free).update(
                prior_status=_seen(F("status"), ThreadRunStatus.FAILED),
                prior_reason=_seen(F("reason"), RunEndReason.INTERRUPTED),
                prior_waiting=F("waiting"),
                prior_last_run_id=F("last_run_id"),
                status=ThreadRunStatus.RUNNING,
                reason=None,
                waiting=None,
                active_run_id=run_id,
                last_run_id=run_id,
                lease_expires_at=now + lease,
                cancel_requested_at=None,
                updated_at=now,
            )
            if not won and resumes is not None:
                _refuse_resume(rows, now, thread_id, resumes)
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

    def _release(self, thread_id: str, run_id: str, request: HttpRequest) -> bool:
        owner_id = self._owner(request)
        now = self._clock()
        # A claim taken before the prior columns existed has no state to restore.
        held = _held(owner_id, thread_id, run_id, now).filter(prior_status__isnull=False)
        with transaction.atomic():
            unknown, _ = held.filter(prior_last_run_id__isnull=True).delete()
            released = unknown or held.update(
                status=F("prior_status"),
                reason=F("prior_reason"),
                waiting=F("prior_waiting"),
                last_run_id=F("prior_last_run_id"),
                active_run_id=None,
                lease_expires_at=None,
                cancel_requested_at=None,
                prior_status=None,
                prior_reason=None,
                prior_waiting=None,
                prior_last_run_id=None,
                updated_at=now,
            )
            if released:
                _log(owner_id, run_id).delete()
        return bool(released)

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
        waiting: tuple[RunWaiting, ...],
        request: HttpRequest,
    ) -> bool:
        owner_id = self._owner(request)
        now = self._clock()
        status, reason = _ENDINGS[outcome]
        with transaction.atomic():
            finished = _held(owner_id, thread_id, run_id, now).update(
                status=status,
                reason=reason,
                waiting=[asdict(call) for call in waiting] or None,
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
        holder = StoredThreadRun.objects.filter(
            owner_id=owner_id,
            thread_id=OuterRef("thread_id"),
            active_run_id=run_id,
            lease_expires_at__gte=now,
        )
        # PostgreSQL re-checks a blocked UPDATE against the log row, not the EXISTS.
        log = _log(owner_id, run_id).filter(ended_at__isnull=True)
        with transaction.atomic():
            bumped = _bump(log, Exists(holder), len(events), now)
            if bumped is None:
                return None
            log_id, next_seq = bumped
            first = next_seq - len(events)
            StoredRunEvent.objects.bulk_create(
                StoredRunEvent(log_id=log_id, seq=first + offset, event=event, created_at=now)
                for offset, event in enumerate(events)
            )
        return next_seq - 1

    def _moved(self, run_id: str, after: int, request: HttpRequest) -> bool:
        row = _log(self._owner(request), run_id).values_list("next_seq", "ended_at").first()
        return row is None or row[0] > after + 1 or row[1] is not None

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
        stale = stale_run_logs(self._clock(), self._log_retention)
        pks = list(stale.values_list("pk", flat=True))
        StoredRunLog.objects.filter(pk__in=pks).delete()
        return len(pks)


def _thread(owner_id: str, thread_id: str) -> QuerySet[StoredThreadRun]:
    return StoredThreadRun.objects.filter(owner_id=owner_id, thread_id=thread_id)


def _held(owner_id: str, thread_id: str, run_id: str, now: datetime) -> QuerySet[StoredThreadRun]:
    """The thread's row while ``run_id`` holds it under a live lease."""
    return _thread(owner_id, thread_id).filter(active_run_id=run_id, lease_expires_at__gte=now)


def _refuse_resume(
    rows: QuerySet[StoredThreadRun], now: datetime, thread_id: str, resumes: str
) -> NoReturn:
    """Raise ``RunNotParkedError``, naming the thread's live holder if it has one."""
    holder = rows.filter(active_run_id__isnull=False, lease_expires_at__gte=now)
    raise RunNotParkedError(
        f"thread {thread_id!r} is not waiting on run {resumes!r}",
        run_id=resumes,
        active_run_id=holder.values_list("active_run_id", flat=True).first(),
    )


def _seen(column: F, lapsed: str) -> Case:
    """The column as ``get`` read it; a holder a claim displaces is always a lapsed one."""
    return Case(
        When(active_run_id__isnull=False, then=Value(lapsed)),
        default=column,
        output_field=CharField(),
    )


def _returns_from_update(connection: BaseDatabaseWrapper) -> bool:
    """Whether ``connection`` takes the ``UPDATE ... RETURNING`` that ``_bump`` writes."""
    if connection.vendor == "sqlite":
        return connection.features.can_return_rows_from_bulk_insert
    return connection.vendor == "postgresql"


def _bump(
    log: QuerySet[StoredRunLog], held: Exists, count: int, now: datetime
) -> tuple[int, int] | None:
    """Advance ``next_seq`` by ``count`` while ``held``; the log's pk and new ``next_seq``."""
    db = router.db_for_write(StoredRunLog)
    connection = connections[db]
    if not _returns_from_update(connection):
        if not log.filter(held).update(next_seq=F("next_seq") + count, updated_at=now):
            return None
        return log.values_list("pk", "next_seq").get()
    query = cast("UpdateQuery", log.filter(held).query.chain(UpdateQuery))
    query.add_update_values({"next_seq": F("next_seq") + count, "updated_at": now})
    sql, params = query.get_compiler(db).as_sql()
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute(f"{sql} RETURNING {quote('id')}, {quote('next_seq')}", params)
        row = cursor.fetchone()
    return None if row is None else (row[0], row[1])


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
        waiting=tuple(RunWaiting(**call) for call in row.waiting or ()),
        active_run_id=active_run_id,
        last_run_id=row.last_run_id,
        lease_expires_at=row.lease_expires_at,
        cancel_requested=row.cancel_requested_at is not None,
        last_read_at=row.last_read_at,
    )


__all__ = ["DefaultRunStore"]
