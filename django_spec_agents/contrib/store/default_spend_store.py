from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import BigIntegerField, Case, F, Q, Value, When
from django.utils import timezone
from pydantic_ai_harness.spend_limits import SpendEntry, Spent

from django_spec_agents.contrib.store.models import StoredSpendCounter, StoredSpendMarker

logger = logging.getLogger(__name__)

_NANOS = Decimal(10) ** 9
_COUNTERS = ("usd_nanos", "tokens", "requests", "unpriced")
_BIG = BigIntegerField()


class DefaultSpendStore:
    """A ``pydantic-ai-harness`` ``BatchSpendStore`` over the reference models, so every
    worker on one database enforces one budget.

    The Django-model counterpart of the harness's ``PostgresSpendStore``, on any database
    Django supports. Built once and shared: it holds no request and scopes nothing, since a
    budget's ``scope`` already names the partition in each key. Every window of a response
    is applied in one transaction, so all of them count or none does, and a
    ``SpendEntry.token`` already applied to a window within ``dedup_retain`` is not counted
    again. A window past its horizon reads as zero and restarts at the next write.

    Horizons are measured on ``clock``, the application's clock (``timezone.now`` unless
    given), not the database's, so workers whose clocks disagree disagree on when a window
    rolls over. A read and the increment after it are separate statements, so concurrent
    runs can each find a budget unspent and pass it together, as with every harness store.
    Every ``sweep_every`` entries this instance writes, it deletes the expired rows.

    Add ``"django_spec_agents.contrib.store"`` to ``INSTALLED_APPS`` and run ``migrate``
    for the backing tables. Requires the ``django-spec-agents[harness]`` extra.
    """

    def __init__(
        self,
        *,
        dedup_retain: timedelta | None = timedelta(hours=1),
        sweep_every: int = 256,
        clock: Callable[[], datetime] = timezone.now,
    ) -> None:
        """``dedup_retain=None`` applies every entry, a replayed one included."""
        self._dedup_retain = dedup_retain
        self._sweep_every = sweep_every
        self._clock = clock
        self._writes_since_sweep = 0

    async def get_many(self, keys: Sequence[str]) -> Mapping[str, Spent]:
        """What each key has accumulated; an absent or expired window reads as zero."""
        return await sync_to_async(self._get_many)(keys)

    async def add_many(self, entries: Sequence[SpendEntry]) -> Mapping[str, Spent]:
        """Apply every entry in one transaction and return each key's new total."""
        return await sync_to_async(self._add_many)(entries)

    def _get_many(self, keys: Sequence[str]) -> dict[str, Spent]:
        return _totals(keys, self._clock())

    def _add_many(self, entries: Sequence[SpendEntry]) -> dict[str, Spent]:
        if not entries:
            return {}
        with transaction.atomic():
            now = self._clock()
            order = range(len(entries))
            by_marker = sorted(order, key=lambda i: (entries[i].key, entries[i].token or ""))
            claimed = {i for i in by_marker if self._claim(entries[i], now)}
            for i in sorted(order, key=lambda i: entries[i].key):
                if i in claimed:
                    _increment(entries[i], now)
            totals = _totals([entry.key for entry in entries], now)
        self._writes_since_sweep += len(entries)
        if self._writes_since_sweep >= self._sweep_every:
            self._writes_since_sweep = 0
            _sweep(self._clock())
        return totals

    def _claim(self, entry: SpendEntry, now: datetime) -> bool:
        """Whether this entry counts: no token, or one not applied to its window yet."""
        if entry.token is None or self._dedup_retain is None:
            return True
        horizon = self._dedup_retain if entry.ttl is None else min(self._dedup_retain, entry.ttl)
        marker = _digest(f"{len(entry.key)}:{entry.key}{entry.token}")
        _, created = StoredSpendMarker.objects.get_or_create(
            marker_sha256=marker, defaults={"expires_at": now + horizon}
        )
        stale = StoredSpendMarker.objects.filter(marker_sha256=marker, expires_at__lte=now)
        return created or stale.update(expires_at=now + horizon) == 1


def _increment(entry: SpendEntry, now: datetime) -> None:
    digest = _digest(entry.key)
    expires_at = None if entry.ttl is None else now + entry.ttl
    added = dict(zip(_COUNTERS, _amounts(entry), strict=True))
    restarted = {
        column: Case(When(expires_at__lte=now, then=Value(0)), default=F(column), output_field=_BIG)
        + amount
        for column, amount in added.items()
    }
    StoredSpendCounter.objects.get_or_create(
        key_sha256=digest, defaults={"key": entry.key, "expires_at": expires_at}
    )
    StoredSpendCounter.objects.filter(key_sha256=digest).update(**restarted, expires_at=expires_at)


def _amounts(entry: SpendEntry) -> tuple[int, int, int, int]:
    with localcontext() as context:
        context.prec = 40
        nanos = int((entry.usd * _NANOS).to_integral_value(rounding=ROUND_HALF_UP))
    return nanos, entry.tokens, entry.requests, entry.unpriced


def _totals(keys: Sequence[str], now: datetime) -> dict[str, Spent]:
    digests = {_digest(key): key for key in keys}
    live = Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    found: dict[str, Spent] = {}
    for row in StoredSpendCounter.objects.filter(live, key_sha256__in=list(digests)):
        with localcontext() as context:
            context.prec = 40
            usd = Decimal(row.usd_nanos) / _NANOS
        found[digests[row.key_sha256]] = Spent(
            usd=usd, tokens=row.tokens, requests=row.requests, unpriced_requests=row.unpriced
        )
    return {key: found.get(key, Spent()) for key in keys}


def _sweep(now: datetime) -> None:
    try:
        _sweep_rows(now)
    except Exception:
        logger.warning("DefaultSpendStore could not delete expired rows.", exc_info=True)


def _sweep_rows(now: datetime) -> None:
    StoredSpendCounter.objects.filter(expires_at__lte=now).delete()
    StoredSpendMarker.objects.filter(expires_at__lte=now).delete()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


__all__ = ["DefaultSpendStore"]
