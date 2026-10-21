from __future__ import annotations

from enum import Enum


class ToolCategory(str, Enum):
    """Coarse grouping for a tool, surfaced to the agent and the UI.

    Advisory metadata: it lets a frontend group tools, a system prompt reason
    about capability classes, and a project apply category-wide policy. It does
    **not** gate execution; that is the ``destructive`` flag's job.
    """

    SHELL = "shell"
    INTROSPECT = "introspect"
    NAV = "nav"
    UI_READ = "ui_read"
    UI_WRITE = "ui_write"
    UI_GENERIC = "ui_generic"
    OTHER = "other"


__all__ = ["ToolCategory"]
