from __future__ import annotations

import asyncio
from typing import IO, Any, cast

from asgiref.sync import sync_to_async
from django.http import HttpRequest
from pydantic_ai.messages import ToolReturn
from pydantic_ai.toolsets.function import FunctionToolset

from django_spec_agents.agent.types.attachment_inline_config import AttachmentInlineConfig
from django_spec_agents.agent.utils import (
    as_text,
    attached_file,
    is_textual,
    over_limit_note,
    unread_note,
)
from django_spec_agents.persistence.types.attachment_store import AttachmentStore
from django_spec_agents.persistence.types.opened_attachment import OpenedAttachment


def build_attachment_toolset(
    store: AttachmentStore,
    request: HttpRequest,
    *,
    inline: AttachmentInlineConfig | None = None,
) -> FunctionToolset[None]:
    """Build a per-request toolset exposing ``read_attachment`` over ``store``."""
    resolved = AttachmentInlineConfig() if inline is None else inline

    async def read_attachment(attachment_id: str) -> str | ToolReturn:
        """Read a file the user attached to this conversation.

        Pass the ``id`` from an attachment chip on the user's message. A textual
        file comes back as its text. A binary file whose type can be read —
        a PDF or an image — comes back as attached file content, so read it
        directly rather than asking the user to send it again. A file that is
        too large, or of a type that cannot be read, comes back as a short note
        giving its name, type and size.

        **Attached file content lasts for the turn that asked for it.** Whether
        it survives into a later turn depends on how the caller stores history,
        and under some transports it does not. So if an earlier turn read a file
        and you no longer have its contents, call this again with the same id
        rather than saying you cannot see it — the file is still there.
        """
        read = asyncio.ensure_future(_open_and_render(store, request, attachment_id, resolved))
        read.add_done_callback(_retrieve)
        return await asyncio.shield(read)

    return FunctionToolset([cast("Any", read_attachment)], id="django-spec-agents-attachments")


async def _open_and_render(
    store: AttachmentStore, request: HttpRequest, attachment_id: str, inline: AttachmentInlineConfig
) -> str | ToolReturn:
    """Open and render as one task, so a cancelled caller cannot strand the handle."""
    opened = await store.open(attachment_id, request=request)
    if opened is None:
        return f"No attachment with id {attachment_id!r} is available."
    return await sync_to_async(_render)(opened, inline)


def _retrieve(task: asyncio.Future[Any]) -> None:
    """Retrieve a shielded read's failure, which a cancelled caller never awaits."""
    task.cancelled() or task.exception()


def _render(opened: OpenedAttachment, inline: AttachmentInlineConfig) -> str | ToolReturn:
    """Decoded text, else the bytes as file content, else a one-line manifest."""
    ref = opened.ref
    textual = is_textual(ref.mime)
    with opened.content as handle:
        if not (textual or ref.mime in inline.media_types):
            return unread_note(ref.name, ref.mime, f"{ref.size} bytes")
        if ref.size is not None and ref.size > inline.max_bytes:
            return over_limit_note(
                ref.name, ref.mime, f"{ref.size} bytes", inline.max_bytes, text=textual
            )
        data = _read_up_to(handle, inline.max_bytes + 1)
    if len(data) > inline.max_bytes:
        size = f"at least {len(data)} bytes"
        return over_limit_note(ref.name, ref.mime, size, inline.max_bytes, text=textual)
    text = as_text(data, ref.mime)
    if text is not None:
        return text
    if ref.mime in inline.media_types:
        again = " Call read_attachment again with the same id if a later turn needs them."
        return attached_file(data, name=ref.name, mime=ref.mime, again=again)
    return unread_note(ref.name, ref.mime, f"{len(data)} bytes")


def _read_up_to(handle: IO[bytes], limit: int) -> bytes:
    """Up to ``limit`` bytes, looping because a raw stream may return short reads."""
    chunks: list[bytes] = []
    taken = 0
    while taken < limit and (chunk := handle.read(limit - taken)):
        chunks.append(chunk)
        taken += len(chunk)
    return b"".join(chunks)


__all__ = ["build_attachment_toolset"]
