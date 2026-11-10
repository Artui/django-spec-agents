from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AttachmentInlineConfig:
    """Which attachment types ``read_attachment`` hands back as file content."""

    media_types: frozenset[str] = frozenset(
        {
            "application/pdf",
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
        }
    )
    """Content types whose bytes are attached to the tool result; anything
    outside the set falls back to the one-line note."""

    max_bytes: int = 4 * 1024 * 1024
    """Largest file, in bytes, returned rather than described -- text and binary
    alike. Measured against the bytes the store returns, not the declared
    ``AttachmentRef.size``."""


__all__ = ["AttachmentInlineConfig"]
