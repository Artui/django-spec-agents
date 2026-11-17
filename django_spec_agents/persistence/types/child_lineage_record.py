from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django_spec_agents.persistence.types.thread_ref import ThreadRef


@dataclass(frozen=True)
class ChildLineageRecord:
    """What a child thread keeps of its lineage, so a later run of it rebuilds the same bounds.

    ``root`` and ``depth`` place it in its tree, ``tools`` and ``gated`` are its parent's
    tools and the ones the parent gated. The parent itself is ``Conversation.parent``.
    """

    root: ThreadRef
    depth: int
    tools: frozenset[str]
    gated: frozenset[str] = frozenset()

    def to_json(self) -> dict[str, Any]:
        """The record as the JSON a store keeps, its sets as sorted lists."""
        return {
            "root": {"thread_id": self.root.thread_id, "scope": self.root.scope},
            "depth": self.depth,
            "tools": sorted(self.tools),
            "gated": sorted(self.gated),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ChildLineageRecord:
        """The record a store kept with ``to_json``."""
        root = data["root"]
        return cls(
            root=ThreadRef(root["thread_id"], root["scope"]),
            depth=data["depth"],
            tools=frozenset(data["tools"]),
            gated=frozenset(data["gated"]),
        )


__all__ = ["ChildLineageRecord"]
