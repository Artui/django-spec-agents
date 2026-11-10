from __future__ import annotations

from typing import Protocol, runtime_checkable

from django.core.files.uploadedfile import UploadedFile
from django.http import HttpRequest

from django_spec_agents.persistence.types.attachment_ref import AttachmentRef
from django_spec_agents.persistence.types.opened_attachment import OpenedAttachment


@runtime_checkable
class AttachmentStore(Protocol):
    """Pluggable server-side storage for files a user attaches to a conversation."""

    async def save(self, upload: UploadedFile, *, request: HttpRequest) -> AttachmentRef: ...
    async def open(
        self, attachment_id: str, *, request: HttpRequest
    ) -> OpenedAttachment | None: ...
    async def delete(self, attachment_id: str, *, request: HttpRequest) -> None: ...


__all__ = ["AttachmentStore"]
