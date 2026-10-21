"""Rows the store tests share."""

from __future__ import annotations

from django.core.files.base import ContentFile

from django_spec_agents.contrib.store.models import StoredAttachment


def stored_attachment(attachment_id: str, owner_id: str = "7") -> StoredAttachment:
    """An attachment row with a five-byte blob in the default storage."""
    row = StoredAttachment(attachment_id=attachment_id, owner_id=owner_id, size=5)
    row.file.save(attachment_id, ContentFile(b"hello"), save=False)
    row.save()
    return row
