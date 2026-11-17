from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThreadRef:
    """A thread in any scope: ``thread_id`` as the client sees it, and the scope it lives in.

    ``scope`` is the agent id or ``ScopedConversationStore`` scope, ``None`` for an unscoped store.
    """

    thread_id: str
    scope: str | None = None


__all__ = ["ThreadRef"]
