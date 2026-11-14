from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any, cast

from django.core.files.base import File
from django.db.models import Field, Model, QuerySet
from django.http import HttpRequest

from django_spec_agents.contrib.store.models import StoredAttachment
from django_spec_agents.contrib.store.types.attachment_deletion import AttachmentDeletion
from django_spec_agents.persistence.anonymous_operation_error import AnonymousOperationError
from django_spec_agents.persistence.identifier_too_long_error import IdentifierTooLongError
from django_spec_agents.persistence.utils import resolve_owner_id

# 64 KiB: Django's own default read size for a ``File``.
_CHUNK_SIZE = 64 * 1024


def owner_or_none(request: HttpRequest, *, allow_anonymous: bool) -> str | None:
    """The resolved owner id, or ``None`` where ``resolve_owner_id`` refuses."""
    try:
        return resolve_owner_id(request, allow_anonymous=allow_anonymous)
    except AnonymousOperationError:
        return None


def check_identifiers(model: type[Model], **values: str | None) -> None:
    """Raise ``IdentifierTooLongError`` for a value longer than its column on ``model``."""
    for name, value in values.items():
        field = cast("Field[Any, Any]", model._meta.get_field(name))
        limit = cast("int", field.max_length)
        if value is not None and len(value) > limit:
            raise IdentifierTooLongError(
                f"{name} is {len(value)} characters, over its {limit}-character limit"
            )


def hash_file(handle: File) -> str:
    """The SHA-256 of ``handle``'s bytes, as hex, read a chunk at a time."""
    digest = hashlib.sha256()
    for chunk in handle.chunks(chunk_size=_CHUNK_SIZE):
        digest.update(chunk)
    return digest.hexdigest()


def unreferenced_attachments(attachments: QuerySet[StoredAttachment]) -> QuerySet[StoredAttachment]:
    """Narrow ``attachments`` to those no conversation refers to."""
    return attachments.filter(conversations__isnull=True)


def delete_attachments(attachments: Iterable[StoredAttachment]) -> AttachmentDeletion:
    """Delete each row, and its blob once no surviving row shares that blob."""
    rows = 0
    blobs = 0
    freed = 0
    for attachment in attachments:
        stored_name = attachment.file.name
        size = attachment.size
        attachment.delete()
        rows += 1
        if not stored_name or StoredAttachment.objects.filter(file=stored_name).exists():
            continue
        attachment.file.delete(save=False)
        blobs += 1
        freed += size
    return AttachmentDeletion(rows=rows, blobs=blobs, bytes_freed=freed)


def preview_attachment_deletion(attachments: list[StoredAttachment]) -> AttachmentDeletion:
    """What ``delete_attachments`` would remove, removing nothing."""
    sizes: dict[str, int] = {}
    for attachment in attachments:
        if attachment.file.name:
            sizes.setdefault(attachment.file.name, attachment.size)
    kept = set(
        StoredAttachment.objects.filter(file__in=list(sizes))
        .exclude(pk__in=[attachment.pk for attachment in attachments])
        .values_list("file", flat=True)
    )
    freed = [size for name, size in sizes.items() if name not in kept]
    return AttachmentDeletion(rows=len(attachments), blobs=len(freed), bytes_freed=sum(freed))


__all__ = [
    "check_identifiers",
    "delete_attachments",
    "hash_file",
    "preview_attachment_deletion",
    "unreferenced_attachments",
]
