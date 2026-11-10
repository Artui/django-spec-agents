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
    """A ready-to-use model-backed store over ``StoredAttachment``."""

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
        # Hand back the open storage handle rather than reading the file: both
        # ``FileResponse`` and the tool's ``with`` block stream and close it, so a
        # large attachment never lands in memory whole.
        try:
            content = row.file.open("rb")
        except (FileNotFoundError, ValueError):
            # A row whose bytes are gone is an attachment this caller cannot have,
            # which is what ``open`` already promises to report as ``None`` -- the
            # contract exists so a caller has one branch for "unavailable" and
            # cannot tell a missing file from another owner's. Raising here sends
            # every caller down a path none of them writes: the toolset has a
            # sentence ready for ``None`` and gets an opaque tool failure instead,
            # and a ``FileResponse`` view returns a 500 where it means 404. Logged
            # rather than swallowed, because a missing blob beside a live row is a
            # storage fault worth someone's attention even though this request can
            # do nothing with it.
            #
            # Two ways to have no readable bytes, and the contract draws no line
            # between them: storage has lost the blob (``FileNotFoundError``), or
            # the row never recorded a path for one (``ValueError``, from
            # ``FieldFile``'s own empty-file guard).
            #
            # The owner is deliberately absent from the message. An anonymous
            # store spells it ``anon:<session_key>``, which is the caller's live
            # session credential, and application logs are the wrong place to put
            # one. The attachment id finds the row on its own.
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
