from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx2
from anthropic import AsyncAnthropic
from anthropic.types import (
    InputJSONDelta,
    Message,
    MessageDeltaUsage,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawContentBlockStopEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    RawMessageStopEvent,
    TextBlock,
    TextDelta,
    ToolUseBlock,
    Usage,
)
from anthropic.types.raw_message_delta_event import Delta
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

MODEL_NAME = "claude-opus-4-7"


def _message_start(usage: Usage) -> RawMessageStartEvent:
    message = Message(
        id="msg_1",
        type="message",
        role="assistant",
        model=MODEL_NAME,
        content=[],
        stop_reason=None,
        stop_sequence=None,
        usage=usage,
    )
    return RawMessageStartEvent(type="message_start", message=message)


def text_events(text: str, usage: Usage) -> list[Any]:
    """The SDK's own stream events for one text answer."""
    return [
        _message_start(usage),
        RawContentBlockStartEvent(
            type="content_block_start", index=0, content_block=TextBlock(type="text", text="")
        ),
        RawContentBlockDeltaEvent(
            type="content_block_delta", index=0, delta=TextDelta(type="text_delta", text=text)
        ),
        RawContentBlockStopEvent(type="content_block_stop", index=0),
        RawMessageDeltaEvent(
            type="message_delta",
            delta=Delta(stop_reason="end_turn", stop_sequence=None),
            usage=MessageDeltaUsage(output_tokens=2),
        ),
        RawMessageStopEvent(type="message_stop"),
    ]


def tool_use_events(tool_name: str, args: dict[str, Any], usage: Usage) -> list[Any]:
    """The SDK's own stream events for one tool call."""
    block = ToolUseBlock(type="tool_use", id="toolu_1", name=tool_name, input={})
    delta = InputJSONDelta(type="input_json_delta", partial_json=json.dumps(args))
    return [
        _message_start(usage),
        RawContentBlockStartEvent(type="content_block_start", index=0, content_block=block),
        RawContentBlockDeltaEvent(type="content_block_delta", index=0, delta=delta),
        RawContentBlockStopEvent(type="content_block_stop", index=0),
        RawMessageDeltaEvent(
            type="message_delta",
            delta=Delta(stop_reason="tool_use", stop_sequence=None),
            usage=MessageDeltaUsage(output_tokens=2),
        ),
        RawMessageStopEvent(type="message_stop"),
    ]


def _sse(event: Any) -> bytes:
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n".encode()


def anthropic_model(
    responses: Sequence[list[Any]],
    bodies: list[dict[str, Any]],
    *,
    stall_after: int | None = None,
) -> AnthropicModel:
    """An ``AnthropicModel`` over a mock transport serving ``responses`` in turn."""
    queue = list(responses)

    async def stream(events: list[Any]) -> AsyncIterator[bytes]:
        for index, event in enumerate(events):
            if index == stall_after:
                await asyncio.sleep(3600)
            yield _sse(event)

    async def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        headers = {"content-type": "text/event-stream"}
        return httpx2.Response(200, content=stream(queue.pop(0)), headers=headers)

    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    client = AsyncAnthropic(api_key="test", http_client=http_client)
    return AnthropicModel(MODEL_NAME, provider=AnthropicProvider(anthropic_client=client))
