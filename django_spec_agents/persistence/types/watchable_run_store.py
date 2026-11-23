from __future__ import annotations

from datetime import timedelta
from typing import Protocol, runtime_checkable

from django.http import HttpRequest

from django_spec_agents.persistence.types.run_store import RunStore


@runtime_checkable
class WatchableRunStore(RunStore, Protocol):
    """A run store a reader can wait on, rather than reading the log again and again.

    A [`RunStore`][django_spec_agents.RunStore] with one more method.

    Optional: a store written against ``RunStore`` alone stays one, and a
    reader checks ``isinstance(store, WatchableRunStore)`` before it calls
    ``wait``, falling back to its own polling of ``read`` otherwise.
    [`DefaultRunStore`][django_spec_agents.contrib.store.default_run_store.DefaultRunStore]
    is one.
    """

    async def wait(
        self, run_id: str, *, after: int, timeout: timedelta, request: HttpRequest
    ) -> bool:
        """Return once the run's log has an entry numbered above ``after``, or the run ended.

        ``True`` then, and at once when either already holds; ``False`` when
        ``timeout`` passes with neither. A run with no log (never claimed by
        this owner, released or pruned) answers ``True`` at once, so the
        reader goes back to ``read`` and ``get`` rather than waiting on
        nothing. A run whose owner died without finishing is not ended: the
        reader's timeout and ``get``, which reads its lapsed lease as failed,
        cover it.
        """
        ...


__all__ = ["WatchableRunStore"]
