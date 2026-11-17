from __future__ import annotations

CHILD_SESSION_TOOL_NAMES = frozenset(
    {"start_session", "list_tasks", "message_task", "answer_task", "cancel_task", "ask_parent"}
)

__all__ = ["CHILD_SESSION_TOOL_NAMES"]
