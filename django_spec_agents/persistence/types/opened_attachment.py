from __future__ import annotations

from dataclasses import dataclass
from typing import IO

from django_spec_agents.persistence.types.attachment_ref import AttachmentRef


@dataclass(frozen=True)
class OpenedAttachment:
    """An attachment's metadata paired with a readable byte stream."""

    ref: AttachmentRef
    content: IO[bytes]


__all__ = ["OpenedAttachment"]
