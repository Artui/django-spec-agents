from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Content-part types that can carry a file. One of these holds bytes only when
# its source is inline data; a source pointing at a URL costs the row nothing.
_FILE_PART_TYPES = frozenset({"audio", "document", "image", "video"})


def strip_inline_binary_parts(messages: Any) -> tuple[Any, bool]:
    """Rebuild ``messages`` without any inline file bytes, and say if it changed."""
    if not isinstance(messages, list):
        return messages, False
    rebuilt: list[Any] = []
    changed = False
    for message in messages:
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            rebuilt.append(message)
            continue
        kept = [part for part in content if not _is_inline_binary(part)]
        if len(kept) == len(content):
            rebuilt.append(message)
            continue
        changed = True
        rebuilt.append({**message, "content": kept})
    return rebuilt, changed


def _is_inline_binary(part: Any) -> bool:
    """True for a content part that carries file bytes inline."""
    if not isinstance(part, Mapping) or part.get("type") not in _FILE_PART_TYPES:
        return False
    source = part.get("source")
    return isinstance(source, Mapping) and source.get("type") == "data"


__all__ = ["strip_inline_binary_parts"]
