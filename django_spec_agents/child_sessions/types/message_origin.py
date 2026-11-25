from __future__ import annotations

from enum import StrEnum


class MessageOrigin(StrEnum):
    """The values of ``MESSAGE_ORIGIN_KEY``: the wake line, a child's report or its question."""

    WAKE = "wake"
    TASK_OUTPUT = "task_output"
    TASK_QUESTION = "task_question"


__all__ = ["MessageOrigin"]
