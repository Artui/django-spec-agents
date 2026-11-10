from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AttachmentDeletion:
    """What one attachment deletion pass removed."""

    rows: int
    blobs: int
    bytes_freed: int


__all__ = ["AttachmentDeletion"]
