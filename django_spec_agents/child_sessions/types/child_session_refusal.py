from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChildSessionRefusal:
    """What a child-session tool returns in place of acting: which rule refused, and why."""

    refused: str
    reason: str


__all__ = ["ChildSessionRefusal"]
