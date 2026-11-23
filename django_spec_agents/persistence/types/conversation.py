from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django_spec_agents.persistence.types.child_lineage_record import ChildLineageRecord
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
    the thread that started this one as a child session, and ``lineage`` the bounds it
    runs under; a store that keeps either writes it when given and leaves it alone when
    it is ``None``. ``routine_id`` is the routine whose firing started the thread, kept
    the same way.
    """

    thread_id: str
    messages: list[Any] = field(default_factory=list)
    owner_id: str | None = None
    parent: ThreadRef | None = None
    lineage: ChildLineageRecord | None = None
    routine_id: str | None = None


__all__ = ["Conversation"]
