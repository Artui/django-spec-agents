from __future__ import annotations

from enum import StrEnum


class InboundKind(StrEnum):
    """What an inbound message carries to a thread: a child's report or question, or input."""

    REPORT = "report"
    QUESTION = "question"
    ANSWER = "answer"
    STEER = "steer"


__all__ = ["InboundKind"]
