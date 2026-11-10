from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel


def tool_calling_model(
    tool_name: str,
    args: Mapping[str, Any] | None = None,
    *,
    retry_args: Mapping[str, Any] | None = None,
    final_text: str = "done",
) -> FunctionModel:
    """A model that calls one tool, optionally corrects itself once, then stops."""

    def model_fn(messages: Any, info: Any) -> ModelResponse:
        return _next_response(messages, tool_name, args, retry_args, final_text)

    return FunctionModel(model_fn)


def _next_response(
    messages: Any,
    tool_name: str,
    args: Mapping[str, Any] | None,
    retry_args: Mapping[str, Any] | None,
    final_text: str,
) -> ModelResponse:
    """What the double answers next, given the run so far."""
    first: dict[str, Any] = dict(args or {})
    second: dict[str, Any] = dict(retry_args) if retry_args is not None else dict(first)

    last = messages[-1]
    if any(_is_retry(part) for part in last.parts):
        return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=second)])
    if any(part.part_kind == "tool-return" for part in last.parts):
        return ModelResponse(parts=[TextPart(final_text)])
    return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=first)])


def _is_retry(part: Any) -> bool:
    """Whether ``part`` asks the model to make its call again, in either shape."""
    return part.part_kind == "retry-prompt" or getattr(part, "outcome", None) == "retried"


def streaming_tool_calling_model(
    tool_name: str,
    args: Mapping[str, Any] | None = None,
    *,
    retry_args: Mapping[str, Any] | None = None,
    final_text: str = "done",
) -> FunctionModel:
    """`tool_calling_model`, for a transport that streams."""

    def model_fn(messages: Any, info: Any) -> ModelResponse:
        return _next_response(messages, tool_name, args, retry_args, final_text)

    async def stream_fn(messages: Any, info: Any) -> AsyncIterator[Any]:
        # One part, so unpacked: a loop would carry an arm no run can reach.
        (part,) = _next_response(messages, tool_name, args, retry_args, final_text).parts
        if isinstance(part, ToolCallPart):
            yield {0: DeltaToolCall(name=part.tool_name, json_args=part.args_as_json_str())}
            return
        assert isinstance(part, TextPart), f"the double produced an unstreamable part: {part!r}"
        yield part.content

    return FunctionModel(model_fn, stream_function=stream_fn)


def instruction_capturing_model(
    captured: dict[str, Any], *, final_text: str = "done"
) -> FunctionModel:
    """A model that records the instructions it was given and calls nothing."""

    def model_fn(messages: Any, info: Any) -> ModelResponse:
        captured["instructions"] = messages[-1].instructions
        return ModelResponse(parts=[TextPart(final_text)])

    return FunctionModel(model_fn)


__all__ = [
    "instruction_capturing_model",
    "streaming_tool_calling_model",
    "tool_calling_model",
]
