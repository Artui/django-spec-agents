from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Conversation:
    """A persisted conversation, keyed by ``thread_id``."""

    thread_id: str
    messages: list[Any] = field(default_factory=list)
    owner_id: str | None = None


__all__ = ["Conversation"]
