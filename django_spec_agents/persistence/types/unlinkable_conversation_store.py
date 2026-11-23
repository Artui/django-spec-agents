from __future__ import annotations

from typing import Protocol, runtime_checkable

from django.http import HttpRequest

from django_spec_agents.persistence.types.conversation_store import ConversationStore


@runtime_checkable
class UnlinkableConversationStore(ConversationStore, Protocol):
    """A conversation store that can clear a thread's parent link.

    A [`ConversationStore`][django_spec_agents.ConversationStore] with one more
    method. Optional: a store written against ``ConversationStore`` alone stays
    one, and a transport checks ``isinstance(store, UnlinkableConversationStore)``
    before it calls ``unlink_parent``.
    [`DefaultConversationStore`][django_spec_agents.contrib.store.default_conversation_store.DefaultConversationStore]
    is one, and so is a
    [`ScopedConversationStore`][django_spec_agents.ScopedConversationStore] over
    one.
    """

    async def unlink_parent(self, thread_id: str, *, request: HttpRequest) -> None:
        """Clear the owner's thread's ``parent`` and ``lineage``, leaving an ordinary thread.

        Owner-scoped like every other method: a missing thread, or another
        owner's, is left alone. Saving the thread again with a ``parent`` links it
        again, so a caller unlinks a child once its run has ended.
        """
        ...


__all__ = ["UnlinkableConversationStore"]
