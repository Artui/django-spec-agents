from __future__ import annotations

import io
import json
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django_service_specs import (
    FilePresenter,
    FileResult,
    SelectorKind,
    SelectorSpec,
    ServiceSpec,
)
from pydantic_ai import Agent, ToolFailed
from pydantic_ai.messages import BinaryContent, ImageUrl, ModelRequest, ToolReturn, UserPromptPart

from django_spec_agents import AgentDeps
from django_spec_agents.agent.types.attachment_inline_config import AttachmentInlineConfig
from django_spec_agents.spec_tools import SpecToolset
from django_spec_agents.spec_tools.spec_toolset import _enforce_result_bytes
from django_spec_agents.testing import tool_calling_model
from tests.spec_tools.kernel_specs import allow
from tests.tool_retries import tool_answers

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
CEILING = AttachmentInlineConfig().max_bytes


def _file_spec(produce: Any, content_type: str | None = "text/plain") -> ServiceSpec:
    def make(user):
        """Make a file."""
        return produce()

    presenter = None if content_type is None else FilePresenter(content_type)
    return ServiceSpec(service=make, presenter=presenter, permissions=allow(), atomic=False)


async def _run(spec: ServiceSpec, **toolset_kwargs: Any) -> Any:
    toolset = SpecToolset({"make": spec}, **toolset_kwargs)
    agent = Agent(tool_calling_model("make"), deps_type=AgentDeps, toolsets=[toolset])
    return await agent.run("go", deps=AgentDeps(user="alice"))


def _answer(result: Any) -> Any:
    (part,) = tool_answers(result.all_messages())
    return part.content


def _attached(result: Any) -> list[BinaryContent]:
    return [
        item
        for message in result.all_messages()
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and not isinstance(part.content, str)
        for item in part.content
        if isinstance(item, BinaryContent)
    ]


class _Closing(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.reads = 0

    def read(self, size: int | None = -1) -> bytes:
        self.reads += 1
        return super().read(size)


async def test_a_text_file_within_the_ceiling_is_its_text():
    text = 'line "one"\nline two\n'
    result = await _run(_file_spec(lambda: FileResult(text.encode(), "notes.txt")))

    assert _answer(result) == text
    assert _attached(result) == []


async def test_a_media_file_within_the_ceiling_reaches_the_next_request_as_file_content():
    spec = _file_spec(lambda: FileResult(PNG, "chart.png"), content_type="image/png")
    result = await _run(spec)

    assert _answer(result) == (
        f"[chart.png] is a image/png file ({len(PNG)} bytes); its contents are attached "
        "to this tool result, for this turn."
    )
    (attached,) = _attached(result)
    assert attached.data == PNG
    assert attached.media_type == "image/png"


async def test_a_file_over_the_ceiling_is_a_one_line_manifest():
    data = b"x" * (CEILING + 1)
    result = await _run(_file_spec(lambda: FileResult(data, "big.txt")))

    assert _answer(result) == (
        f"[big.txt] is a text/plain file ({CEILING + 1} bytes), over the {CEILING}-byte "
        "limit for returning a file's contents; it was described rather than read."
    )
    assert _attached(result) == []


async def test_a_media_file_of_unknown_size_over_the_ceiling_is_read_no_further_than_it():
    chunks = iter([b"\x89PNG" + b"x" * CEILING, b"never read"])
    result = await _run(_file_spec(lambda: FileResult(chunks, "big.png"), content_type="image/png"))

    assert _answer(result) == (
        f"[big.png] is a image/png file (at least {CEILING + 1} bytes), over the "
        f"{CEILING}-byte limit for attaching a file's contents; it was described rather "
        "than attached."
    )
    assert next(chunks) == b"never read"


async def test_a_file_of_a_type_the_model_cannot_read_is_closed_unread():
    handle = _Closing(b"PK\x03\x04")
    result = await _run(
        _file_spec(lambda: FileResult(handle, "a.zip"), content_type="application/zip")
    )

    assert _answer(result) == (
        "[a.zip] is a application/zip file (size unknown); its content is not text "
        "and was not inlined."
    )
    assert handle.reads == 0
    assert handle.closed


async def test_a_textual_type_that_does_not_decode_is_described_by_its_length():
    result = await _run(_file_spec(lambda: FileResult(b"\xff\xfe", "bad.txt")))

    assert _answer(result) == (
        "[bad.txt] is a text/plain file (2 bytes); its content is not text and was not inlined."
    )


async def test_a_file_with_no_type_is_described_as_binary():
    result = await _run(_file_spec(lambda: FileResult(b"abc", "blob"), content_type=None))

    assert _answer(result) == (
        "[blob] is a binary file (3 bytes); its content is not text and was not inlined."
    )


async def test_the_byte_ceiling_measures_a_text_file_by_its_length_not_its_json():
    text = '"\n' * 20
    assert len(text.encode()) <= 40 < len(json.dumps(text))
    result = await _run(_file_spec(lambda: FileResult(text.encode(), "q.txt")), max_result_bytes=40)

    assert _answer(result) == text


async def test_the_byte_ceiling_measures_a_media_file_by_its_bytes_not_their_repr():
    spec = _file_spec(lambda: FileResult(PNG, "chart.png"), content_type="image/png")
    note = len(f"[chart.png] is a image/png file ({len(PNG)} bytes); its contents are")
    ceiling = len(PNG) + note + 80
    assert (
        len(
            json.dumps(
                ToolReturn("", content=[BinaryContent(data=PNG, media_type="image/png")]),
                default=str,
            )
        )
        > ceiling
    )
    result = await _run(spec, max_result_bytes=ceiling)

    assert _attached(result)[0].data == PNG


async def test_the_byte_ceiling_refuses_a_media_file_over_it():
    spec = _file_spec(lambda: FileResult(PNG, "chart.png"), content_type="image/png")
    toolset = SpecToolset({"make": spec}, max_result_bytes=len(PNG))
    agent = Agent(tool_calling_model("make"), deps_type=AgentDeps, toolsets=[toolset])
    result = await agent.run("go", deps=AgentDeps(user="alice"))

    (part,) = tool_answers(result.all_messages())
    assert part.outcome == "failed"
    assert f"over the {len(PNG)} byte ceiling" in part.content


def _bound(payload: Any, max_bytes: int) -> Any:
    return _enforce_result_bytes(payload, max_bytes=max_bytes, label="t", extra={})


def test_a_raw_file_is_measured_by_its_declared_size():
    file = FileResult(b"abcd", "a.bin")
    assert _bound(file, 4) is file
    with pytest.raises(ToolFailed, match="This result was 4 bytes, over the 3 byte ceiling"):
        _bound(file, 3)


def test_a_raw_file_of_unknown_size_is_read_up_to_the_ceiling():
    kept = _bound(FileResult(iter([b"ab", b"cd"]), "a.bin"), 4)
    assert (kept.content, kept.size) == (b"abcd", 4)
    with pytest.raises(ToolFailed, match="This result was more than 3 bytes, over the 3 byte"):
        _bound(FileResult(iter([b"ab", b"cd"]), "a.bin"), 3)


@pytest.mark.parametrize(
    ("payload", "size"),
    [
        (ToolReturn("é"), 2),
        (ToolReturn({"a": 1}), len('{"a": 1}')),
        (ToolReturn("", content="é"), 2),
        (ToolReturn("", content=[BinaryContent(data=b"abc", media_type="image/png"), "é"]), 5),
        (
            ToolReturn("", content=[ImageUrl(url="https://x.test/a.png")]),
            len(json.dumps(ImageUrl(url="https://x.test/a.png"), default=str)),
        ),
    ],
)
def test_a_tool_return_is_measured_part_by_part(payload: ToolReturn, size: int):
    assert _bound(payload, size) is payload
    with pytest.raises(ToolFailed, match=f"This result was {size} bytes"):
        _bound(payload, size - 1)


def _rows():
    """Rows."""
    return []


def test_a_file_spec_the_kernel_refuses_is_refused_at_registration_naming_the_tool():
    spec = SelectorSpec(
        selector=_rows,
        kind=SelectorKind.LIST,
        presenter=FilePresenter("text/csv", filename="rows.csv"),
        permissions=allow(),
    )
    with pytest.raises(ImproperlyConfigured, match="SpecToolset tool 'export'.*list of rows"):
        SpecToolset({"export": spec})
