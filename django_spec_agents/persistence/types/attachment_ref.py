from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AttachmentRef:
    """A durable, lightweight reference to one uploaded file."""

    id: str
    name: str
    mime: str
    size: int
    url: str | None = None


__all__ = ["AttachmentRef"]
