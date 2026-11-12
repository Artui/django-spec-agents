from __future__ import annotations

import logging
from collections.abc import Iterable
from uuid import uuid4

from django.core.files.uploadedfile import UploadedFile

from django_spec_agents.contrib.store.models import StoredAttachment
from django_spec_agents.contrib.store.utils import delete_attachments, hash_file
from django_spec_agents.persistence.model_attachment_store import ModelAttachmentStore
from django_spec_agents.persistence.types.attachment_ref import AttachmentRef
from django_spec_agents.persistence.types.opened_attachment import OpenedAttachment

logger = logging.getLogger(__name__)


def _live_blob_name(twins: Iterable[StoredAttachment]) -> str | None:
    """The first stored path these rows offer that storage can still produce."""
    seen: set[str] = set()
    for twin in twins:
        name = twin.file.name
        if not name or name in seen:
            continue
        seen.add(name)
        if twin.file.storage.exists(name):
            return name
    return None


class DefaultAttachmentStore(ModelAttachmentStore):
    """A ready-to-use model-backed store over ``StoredAttachment``.

    Bytes live in Django ``Storage`` (filesystem by default, S3 or GCS through
    ``STORAGES``), metadata in a row. Add
    ``"django_spec_agents.contrib.store"`` to ``INSTALLED_APPS``, run
    ``migrate``, and pass an instance to your transport's ``attachment_store=``.
    For a bespoke schema, subclass
    [`ModelAttachmentStore`][django_spec_agents.ModelAttachmentStore]
    instead.

    Every query filters by the ``owner_id`` the base resolves, so one user's id
    never reaches another's file, and the public ``attachment_id`` is an opaque
    UUID kept separate from the storage filename.

    Uploads are **deduplicated by content hash, within one owner**: the same file
    sent into five threads is written to storage once and pointed at five times,
    while still getting a row of its own each time so it keeps the name the
    composer showed. The blob goes when the last row pointing at it does.
    """

    def _save(self, upload: UploadedFile, owner_id: str | None) -> AttachmentRef:
        """Persist one upload, reusing an identical blob this owner already has."""
        attachment_id = uuid4().hex
        name = _basename(upload.name)
        mime = upload.content_type or ""
        size = upload.size or 0
        digest = hash_file(upload)
        owner = owner_id or ""
        row = StoredAttachment(
            attachment_id=attachment_id,
            owner_id=owner,
            name=name,
            mime=mime,
            size=size,
            sha256=digest,
        )
        twins = StoredAttachment.objects.filter(owner_id=owner, sha256=digest).order_by("-pk")
        adopted = _live_blob_name(twins)
        if adopted is not None:
            row.file = adopted
        else:
            row.file.save(attachment_id, upload, save=False)
        row.save()
        return AttachmentRef(id=attachment_id, name=name, mime=mime, size=size)

    def _open(self, attachment_id: str, owner_id: str | None) -> OpenedAttachment | None:
        row = _owned_row(attachment_id, owner_id)
        if row is None:
            return None
        # Hand back the open handle, not the bytes: a large file is never read into memory.
        try:
            content = row.file.open("rb")
        except (FileNotFoundError, ValueError):
            logger.warning(
                "Attachment %s has no readable stored file at %r; reporting it as unavailable.",
                row.attachment_id,
                row.file.name,
            )
            return None
        return OpenedAttachment(
            ref=AttachmentRef(id=row.attachment_id, name=row.name, mime=row.mime, size=row.size),
            content=content,
        )

    def _remove(self, attachment_id: str, owner_id: str | None) -> None:
        """Drop one attachment, keeping the blob if another row still shares it."""
        row = _owned_row(attachment_id, owner_id)
        if row is None:
            return
        delete_attachments([row])


def _owned_row(attachment_id: str, owner_id: str | None) -> StoredAttachment | None:
    """The owner's row for ``attachment_id``; anonymous is stored as ``""``."""
    return StoredAttachment.objects.filter(
        owner_id=owner_id or "", attachment_id=attachment_id
    ).first()


def _basename(name: str | None) -> str:
    """The trailing filename component, stripped of any path; never empty."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return base or "attachment"


__all__ = ["DefaultAttachmentStore"]
