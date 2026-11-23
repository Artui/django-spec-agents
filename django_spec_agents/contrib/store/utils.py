from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any, cast

from django.core.files.base import File
from django.core.management.base import CommandError
from django.db.models import Exists, Field, Model, OuterRef, QuerySet
from django.http import HttpRequest

from django_spec_agents.contrib.store.models import StoredAttachment, StoredRunLog, StoredThreadRun
from django_spec_agents.contrib.store.types.attachment_deletion import AttachmentDeletion
from django_spec_agents.persistence.anonymous_operation_error import AnonymousOperationError
from django_spec_agents.persistence.identifier_too_long_error import IdentifierTooLongError
from django_spec_agents.persistence.utils import resolve_owner_id

# 64 KiB: Django's own default read size for a ``File``.
_CHUNK_SIZE = 64 * 1024
# Age units, as seconds. Hours are the unit a bare number is read in.
_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def owner_or_none(request: HttpRequest, *, allow_anonymous: bool) -> str | None:
    """The resolved owner id, or ``None`` where ``resolve_owner_id`` refuses."""
    try:
        return resolve_owner_id(request, allow_anonymous=allow_anonymous)
    except AnonymousOperationError:
        return None


def check_column_widths(model: type[Model], **values: str | None) -> None:
    """Raise ``IdentifierTooLongError`` for a value longer than its column on ``model``."""
    for name, value in values.items():
        field = cast("Field[Any, Any]", model._meta.get_field(name))
        limit = cast("int", field.max_length)
        if value is not None and len(value) > limit:
            raise IdentifierTooLongError(
                f"{name} is {len(value)} characters, over its {limit}-character limit",
                field=name,
                length=len(value),
                limit=limit,
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


def parse_age(value: str) -> timedelta:
    """An ``--older-than`` of ``30m`` / ``24h`` / ``7d``, or hours when the unit is left off."""
    text = value.strip().lower()
    unit = _UNITS.get(text[-1:])
    amount = text if unit is None else text[:-1]
    try:
        number = float(amount)
    except ValueError:
        raise CommandError(
            f"Could not read --older-than {value!r}. Give a number with an "
            "optional s/m/h/d unit, for example 24h."
        ) from None
    if number < 0:
        raise CommandError("--older-than must not be negative.")
    return timedelta(seconds=number * (_UNITS["h"] if unit is None else unit))


def stale_run_logs(now: datetime, retention: timedelta) -> QuerySet[StoredRunLog]:
    """Run logs last written over ``retention`` ago (their end, if they ended), bar a live run's."""
    live = StoredThreadRun.objects.filter(
        owner_id=OuterRef("owner_id"),
        active_run_id=OuterRef("run_id"),
        lease_expires_at__gte=now,
    )
    return StoredRunLog.objects.filter(updated_at__lt=now - retention).exclude(Exists(live))


__all__ = [
    "check_column_widths",
    "delete_attachments",
    "hash_file",
    "parse_age",
    "preview_attachment_deletion",
    "stale_run_logs",
    "unreferenced_attachments",
]
