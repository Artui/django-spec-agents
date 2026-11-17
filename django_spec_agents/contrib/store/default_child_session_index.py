from __future__ import annotations

from asgiref.sync import sync_to_async
from django.http import HttpRequest

from django_spec_agents.child_sessions.types.child_session import ChildSession
from django_spec_agents.contrib.store.models import StoredConversation
from django_spec_agents.persistence.types.run_store import RunStore
from django_spec_agents.persistence.types.thread_ref import ThreadRef
from django_spec_agents.persistence.types.thread_run_status import ThreadRunStatus
from django_spec_agents.persistence.utils import resolve_owner_id


class DefaultChildSessionIndex:
    """The owner's child sessions from the parent columns and a run store.

    A scoped store's ``scope:thread_id`` is un-keyed at its first ``:``."""

    def __init__(self, run_store: RunStore, *, allow_anonymous: bool = False) -> None:
        self._run_store = run_store
        self._allow_anonymous = allow_anonymous

    async def children(self, parent: ThreadRef, *, request: HttpRequest) -> list[ChildSession]:
        keys = await sync_to_async(self._keys)(request, parent)
        return await self._sessions([_unkey(key) for key in keys], request)

    async def live_count(self, *, request: HttpRequest) -> int:
        keys = await sync_to_async(self._keys)(request, None)
        return sum(child.live for child in await self._sessions([_unkey(k) for k in keys], request))

    def _keys(self, request: HttpRequest, parent: ThreadRef | None) -> list[str]:
        rows = StoredConversation.objects.filter(
            owner_id=resolve_owner_id(request, allow_anonymous=self._allow_anonymous)
        )
        if parent is None:
            rows = rows.filter(parent_thread_id__isnull=False)
        else:
            rows = rows.filter(parent_thread_id=parent.thread_id, parent_scope=parent.scope)
        return list(rows.order_by("created_at", "pk").values_list("thread_id", flat=True))

    async def _sessions(self, threads: list[ThreadRef], request: HttpRequest) -> list[ChildSession]:
        states = await self._run_store.get_many([t.thread_id for t in threads], request=request)
        sessions = []
        for thread in threads:
            state = states.get(thread.thread_id)
            if state is None:
                sessions.append(ChildSession(thread, ThreadRunStatus.IDLE))
            else:
                sessions.append(ChildSession(thread, state.status, state.waiting))
        return sessions


def _unkey(key: str) -> ThreadRef:
    scope, separator, thread_id = key.partition(":")
    return ThreadRef(thread_id, scope) if separator else ThreadRef(key)


__all__ = ["DefaultChildSessionIndex"]
