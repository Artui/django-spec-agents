from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TypeAlias


@dataclass(frozen=True)
class ConversationMeta:
    """Lightweight metadata for one conversation: the thread-drawer row shape."""

    thread_id: str
    title: str
    updated_at: datetime | None = None
    preview: str = ""
    owner_id: str | None = None


# Return type for ``ConversationStore.list``. Aliased because a store names that
# method ``list``, which shadows the builtin inside the class body and would
# break a bare ``list[ConversationMeta]`` return annotation.
ConversationMetaList: TypeAlias = list[ConversationMeta]


__all__ = ["ConversationMeta", "ConversationMetaList"]
