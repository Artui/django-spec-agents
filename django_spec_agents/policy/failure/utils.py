from __future__ import annotations

from pydantic_ai.exceptions import ToolFailed


class PolicyToolFailed(ToolFailed):
    """The ``ToolFailed`` that ``ToolFailurePolicy`` raises in place of a tool's exception."""


__all__ = ["PolicyToolFailed"]
