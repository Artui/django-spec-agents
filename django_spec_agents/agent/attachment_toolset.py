from __future__ import annotations

from typing import Any, cast

from django.http import HttpRequest
from pydantic_ai.messages import ToolReturn
from pydantic_ai.toolsets.function import FunctionToolset

from django_spec_agents.agent.types.attachment_inline_config import AttachmentInlineConfig
from django_spec_agents.agent.utils import render_file
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
        opened = await store.open(attachment_id, request=request)
        if opened is None:
            return f"No attachment with id {attachment_id!r} is available."
        return _render(opened, resolved)

    return FunctionToolset([cast("Any", read_attachment)], id="django-spec-agents-attachments")


def _render(opened: OpenedAttachment, inline: AttachmentInlineConfig) -> str | ToolReturn:
    """Decoded text, else the bytes as file content, else a one-line manifest."""
    ref = opened.ref
    with opened.content as handle:
        data = handle.read()
    return render_file(
        data,
        name=ref.name,
        mime=ref.mime,
        declared_size=ref.size,
        inline=inline,
        again=" Call read_attachment again with the same id if a later turn needs them.",
    )


__all__ = ["build_attachment_toolset"]
