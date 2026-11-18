from __future__ import annotations

from datetime import timedelta
from typing import Protocol, runtime_checkable

from django.http import HttpRequest

from django_spec_agents.persistence.types.run_claim import RunClaim
from django_spec_agents.persistence.types.run_store import RunStore


@runtime_checkable
class ResumableRunStore(RunStore, Protocol):
    """A run store that resumes a parked run once and gives a claim back.

    A [`RunStore`][django_spec_agents.RunStore] with two more pieces.

    Optional: a store written against ``RunStore`` alone stays one, and a
    transport checks ``isinstance(store, ResumableRunStore)`` before it passes
    ``resumes=`` or calls ``release``.
    [`DefaultRunStore`][django_spec_agents.contrib.store.default_run_store.DefaultRunStore]
    is one.
    """

    async def claim(
        self,
        thread_id: str,
        run_id: str,
        *,
        request: HttpRequest,
        lease: timedelta,
        resumes: str | None = None,
    ) -> RunClaim:
        """``RunStore.claim``; with ``resumes``, won only while the thread is parked on that run.

        The condition and the claim are one conditional update, so of two
        claims resuming the same parked run exactly one wins. A refused resume
        answers busy, with the holder, while a live run holds the thread, and
        otherwise raises
        [`RunNotParkedError`][django_spec_agents.RunNotParkedError]: the
        thread is idle, failed, parked on another run, or running under a
        lapsed lease. Either way nothing is written.
        """
        ...

    async def release(self, thread_id: str, run_id: str, *, request: HttpRequest) -> bool:
        """Give back a claim ``run_id`` still holds, as though it had never been taken.

        The thread's status, reason, waiting record and ``last_run_id`` return
        to what they were before the claim, a thread no run had claimed reads
        as unknown again, and the released run's log is deleted, so its id may
        be claimed again. ``False``, with nothing changed, if ``run_id`` is not
        the thread's live holder.
        """
        ...


__all__ = ["ResumableRunStore"]
