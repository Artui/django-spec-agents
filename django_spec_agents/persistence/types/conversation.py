from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django_spec_agents.persistence.types.thread_ref import ThreadRef


@dataclass(frozen=True)
class Conversation:
    """A persisted conversation, keyed by ``thread_id``.

    ``messages`` are **JSON-serialisable records whose shape the calling
    transport owns**: this substrate persists and returns them verbatim and never
    interprets them, which is what keeps the storage contract neutral. The AG-UI
    transport stores its own wire ``Message`` shape, so client message ids
    survive a round trip untouched; another transport stores its own.

    ``owner_id`` scopes the conversation to a user for authorization. ``parent`` is
    the thread that started this one as a child session; a store that keeps it
    writes it when given and leaves the stored link alone when it is ``None``.
    """

    thread_id: str
    messages: list[Any] = field(default_factory=list)
    owner_id: str | None = None
    parent: ThreadRef | None = None


__all__ = ["Conversation"]
