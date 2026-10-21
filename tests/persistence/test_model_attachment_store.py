from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile, UploadedFile

from django_spec_agents.persistence.model_attachment_store import ModelAttachmentStore
from django_spec_agents.persistence.types.attachment_ref import AttachmentRef
from django_spec_agents.persistence.types.opened_attachment import OpenedAttachment
from tests.request_helpers import authed_request


class _DictStore(ModelAttachmentStore):
    """A dict-backed subclass exercising the base's async + owner plumbing."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str | None, str], tuple[AttachmentRef, bytes]] = {}

    def _save(self, upload: UploadedFile, owner_id: str | None) -> AttachmentRef:
        ref = AttachmentRef(
            id=upload.name or "a",
            name=upload.name or "a",
            mime=upload.content_type or "",
            size=upload.size or 0,
        )
        self.rows[(owner_id, ref.id)] = (ref, upload.read())
        return ref

    def _open(self, attachment_id: str, owner_id: str | None) -> OpenedAttachment | None:
        found = self.rows.get((owner_id, attachment_id))
        if found is None:
            return None
        ref, content = found
        return OpenedAttachment(ref=ref, content=content)

    def _remove(self, attachment_id: str, owner_id: str | None) -> None:
        self.rows.pop((owner_id, attachment_id), None)


async def test_base_wraps_sync_ops_with_owner_scoping() -> None:
    store = _DictStore()
    request = authed_request()
    upload = SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")

    ref = await store.save(upload, request=request)
    assert ref.name == "notes.txt"
    assert ("7", "notes.txt") in store.rows  # scoped to the resolved owner id

    opened = await store.open("notes.txt", request=request)
    assert opened is not None
    assert opened.content == b"hello"

    await store.delete("notes.txt", request=request)
    assert await store.open("notes.txt", request=request) is None


async def test_open_missing_returns_none() -> None:
    assert await _DictStore().open("absent", request=authed_request()) is None
