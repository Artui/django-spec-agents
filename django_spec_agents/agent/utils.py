from __future__ import annotations

from pydantic_ai.messages import BinaryContent, ToolReturn

from django_spec_agents.agent.types.attachment_inline_config import AttachmentInlineConfig

# Non-``text/*`` content types whose bytes are still UTF-8 text worth inlining.
_TEXTUAL_MIMES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-yaml",
        "image/svg+xml",
    }
)


def is_textual(mime: str) -> bool:
    """Whether ``mime`` names a type whose bytes are read as UTF-8 text."""
    return mime.startswith("text/") or mime in _TEXTUAL_MIMES


def as_text(content: bytes, mime: str) -> str | None:
    """UTF-8 text for a textual content type, or ``None`` when binary/undecodable."""
    if not is_textual(mime):
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def over_limit_note(name: str, mime: str, size: str, max_bytes: int, *, text: bool) -> str:
    """The note for a file over ``max_bytes``: described rather than read, or attached."""
    if text:
        return (
            f"[{name}] is a {mime} file ({size}), over the {max_bytes}-byte limit for "
            "returning a file's contents; it was described rather than read."
        )
    return (
        f"[{name}] is a {mime} file ({size}), over the {max_bytes}-byte limit for "
        "attaching a file's contents; it was described rather than attached."
    )


def unread_note(name: str, mime: str, size: str) -> str:
    """The note for a file that is neither text nor a type the model reads."""
    return f"[{name}] is a {mime or 'binary'} file ({size}); its content is not text and was not inlined."  # noqa: E501


def attached_file(data: bytes, *, name: str, mime: str, again: str) -> ToolReturn:
    """``data`` attached to the tool result as file content, for this turn."""
    return ToolReturn(
        return_value=(
            f"[{name}] is a {mime} file ({len(data)} bytes); its contents are attached "
            f"to this tool result, for this turn.{again}"
        ),
        content=[BinaryContent(data=data, media_type=mime)],
    )


def render_file(
    data: bytes,
    *,
    name: str,
    mime: str,
    declared_size: int,
    inline: AttachmentInlineConfig,
    again: str,
) -> str | ToolReturn:
    """Decoded text, else the bytes as file content, else a one-line manifest."""
    size = f"{len(data)} bytes"
    text = as_text(data, mime)
    if text is not None:
        if len(data) > inline.max_bytes:
            return over_limit_note(name, mime, size, inline.max_bytes, text=True)
        return text
    if mime in inline.media_types:
        if len(data) <= inline.max_bytes:
            return attached_file(data, name=name, mime=mime, again=again)
        return over_limit_note(name, mime, size, inline.max_bytes, text=False)
    return unread_note(name, mime, f"{declared_size} bytes")
