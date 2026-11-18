from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django_spec_agents.child_sessions.types.inbound_kind import InboundKind
from django_spec_agents.persistence.types.thread_ref import ThreadRef


@dataclass(frozen=True)
class InboundMessage:
    """One message waiting in a thread's inbound queue, ``id`` its place in the queue.

    ``body`` is opaque JSON the store never reads; ``sender`` is the thread it came from, if any;
    ``taken_by`` is the run that took it and has not acked or released it, if any.
    """

    id: int
    kind: InboundKind
    body: Any
    created_at: datetime
    sender: ThreadRef | None = None
    taken_by: str | None = None


__all__ = ["InboundMessage"]
