from __future__ import annotations

from django.core.files.uploadedfile import UploadedFile
from django.http import HttpRequest

from django_spec_agents.persistence.types.attachment_ref import AttachmentRef
from django_spec_agents.persistence.types.opened_attachment import OpenedAttachment


class NullAttachmentStore:
    """The default attachment store: uploads disabled, server stays stateless."""

    async def save(self, upload: UploadedFile, *, request: HttpRequest) -> AttachmentRef:
        raise NotImplementedError(
            "attachments are disabled: configure an attachment store to enable uploads"
        )

    async def open(self, attachment_id: str, *, request: HttpRequest) -> OpenedAttachment | None:
        return None

    async def delete(self, attachment_id: str, *, request: HttpRequest) -> None:
        return None


__all__ = ["NullAttachmentStore"]
