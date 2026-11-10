from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic_ai.messages import ModelMessage


def is_tool_retry(part: Any) -> bool:
    """A part asking the model to make a tool call again."""
    if part.part_kind == "retry-prompt":
        return part.tool_name is not None
    return part.part_kind == "tool-return" and part.outcome == "retried"


def is_tool_answer(part: Any) -> bool:
    """A tool return that is not a retry: the call settled, one way or another."""
    return part.part_kind == "tool-return" and not is_tool_retry(part)


def tool_retries(messages: Iterable[ModelMessage]) -> list[Any]:
    """Every tool retry in ``messages``, in order."""
    return [part for message in messages for part in message.parts if is_tool_retry(part)]


def tool_answers(messages: Iterable[ModelMessage]) -> list[Any]:
    """Every tool return in ``messages`` that is not a retry, in order."""
    return [part for message in messages for part in message.parts if is_tool_answer(part)]
