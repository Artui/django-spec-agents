from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

from django.http import HttpRequest

from django_spec_agents.persistence.types.run_claim import RunClaim
from django_spec_agents.persistence.types.run_log_entry import RunLogEntry
from django_spec_agents.persistence.types.run_outcome import RunOutcome
from django_spec_agents.persistence.types.run_waiting import RunWaiting
from django_spec_agents.persistence.types.thread_run_state import ThreadRunState


@runtime_checkable
class RunStore(Protocol):
    """Where a thread's run status and each run's event log live, apart from its transcript.

    A run that outlives its request (detached from a browser, or started with
    no stream at all) needs both: a status any process can read, and a log a
    reader can catch up on. The transcript, partial output included, stays in
    the [`ConversationStore`][django_spec_agents.ConversationStore]; this
    protocol adds no method to it.

    Keyed like a ``ConversationStore``: every method but ``prune`` takes the
    request, and the store scopes rows by the owner it resolves from it.

    **One run per thread.** ``claim`` takes ``active_run_id`` in one
    conditional update, won when the thread holds no run or its holder's lease
    has lapsed; a second claim loses and learns the holder. The holder keeps
    its claim alive with ``renew`` and gives it up with ``finish``. ``renew``,
    ``append`` and ``finish`` refuse, returning ``False`` or ``None``, a run that
    is not the thread's live holder, so an owner presumed dead cannot overwrite
    its successor.

    **A lapsed lease reads as a failure.** ``get`` reports a ``running`` thread
    whose lease has lapsed as ``failed`` with reason ``interrupted``.

    **The log is append-only and numbered.** ``append`` numbers a batch of
    opaque JSON events on from the run's last, starting at 1; nothing edits or
    removes an entry but ``prune``, which drops whole logs a window after their
    run ended.
    """

    async def claim(
        self, thread_id: str, run_id: str, *, request: HttpRequest, lease: timedelta
    ) -> RunClaim:
        """Take the thread for ``run_id`` with a lease of ``lease``, unless a live run holds it.

        Run ids are single-use per owner: a resumed or retried run claims under
        a fresh id, and an id the owner has claimed before raises
        [`RunIdReusedError`][django_spec_agents.RunIdReusedError] with nothing
        written.
        """
        ...

    async def renew(
        self, thread_id: str, run_id: str, *, request: HttpRequest, lease: timedelta
    ) -> bool:
        """Extend the live holder's lease to ``lease`` from now; ``False`` if ``run_id`` lost it."""
        ...

    async def finish(
        self,
        thread_id: str,
        run_id: str,
        outcome: RunOutcome,
        *,
        request: HttpRequest,
        waiting: RunWaiting | None = None,
    ) -> bool:
        """Release the claim and record how the run ended; ``waiting`` goes with ``WAITING``."""
        ...

    async def append(
        self, run_id: str, events: Sequence[Any], *, request: HttpRequest
    ) -> int | None:
        """Append ``events`` to the run's log, numbered; the last number, or ``None`` if refused."""
        ...

    async def read(
        self, run_id: str, *, request: HttpRequest, after: int = 0, limit: int | None = None
    ) -> list[RunLogEntry]:
        """The run's entries numbered above ``after``, in order, at most ``limit`` of them."""
        ...

    async def get(self, thread_id: str, *, request: HttpRequest) -> ThreadRunState | None:
        """The thread's run state, or ``None`` if no run ever claimed it."""
        ...

    async def get_many(
        self, thread_ids: Sequence[str], *, request: HttpRequest
    ) -> dict[str, ThreadRunState]:
        """The run state of each listed thread that has one, keyed by thread id."""
        ...

    async def request_cancel(self, thread_id: str, *, request: HttpRequest) -> bool:
        """Record a cancel for the thread's live run, for its owner to act on; ``False`` if none."""
        ...

    async def mark_read(self, thread_id: str, *, request: HttpRequest) -> None:
        """Set the thread's ``last_read_at`` to now; a thread no run claimed is left alone."""
        ...

    async def prune(self) -> int:
        """Delete the logs of runs that ended longer ago than the store's window; how many."""
        ...


__all__ = ["RunStore"]
