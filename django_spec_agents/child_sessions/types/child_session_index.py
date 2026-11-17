from __future__ import annotations

from typing import Protocol, runtime_checkable

from django.http import HttpRequest

from django_spec_agents.child_sessions.types.child_session import ChildSession
from django_spec_agents.persistence.types.thread_ref import ThreadRef


@runtime_checkable
class ChildSessionIndex(Protocol):
    """Finds child sessions, for the limits and for cancel: the two questions no store answers.

    Keyed like a [`ConversationStore`][django_spec_agents.ConversationStore], by the owner
    each method resolves from its request. A child's link to its parent is the
    ``parent`` its conversation stores, in any scope, so an index reads those links and
    the children's run status beside them. A child must be listed from the moment
    [`HeadlessRunner.start`][django_spec_agents.HeadlessRunner.start] returns, or two
    starts in a row both pass the per-thread limit.
    """

    async def children(self, parent: ThreadRef, *, request: HttpRequest) -> list[ChildSession]:
        """The sessions ``parent`` started, live or not, oldest first."""
        ...

    async def live_count(self, *, request: HttpRequest) -> int:
        """How many of the owner's child sessions, under any parent, run or wait."""
        ...


__all__ = ["ChildSessionIndex"]
