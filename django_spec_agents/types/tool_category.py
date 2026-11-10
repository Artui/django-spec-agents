from __future__ import annotations

from enum import Enum


class ToolCategory(str, Enum):
    """Coarse grouping for a tool, surfaced to the agent and the UI."""

    SHELL = "shell"
    INTROSPECT = "introspect"
    NAV = "nav"
    UI_READ = "ui_read"
    UI_WRITE = "ui_write"
    UI_GENERIC = "ui_generic"
    OTHER = "other"


__all__ = ["ToolCategory"]
