from __future__ import annotations

from enum import StrEnum


class MessageSource(StrEnum):
    """The values of ``MESSAGE_SOURCE_KEY``: the wake line, a child's report or its question."""

    WAKE = "wake"
    REPORT = "task_output"
    QUESTION = "task_question"


__all__ = ["MessageSource"]
