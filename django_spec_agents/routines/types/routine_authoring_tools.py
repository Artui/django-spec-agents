from __future__ import annotations

ROUTINE_AUTHORING_TOOLS: frozenset[str] = frozenset(
    {"create_routine", "update_routine", "pause_routine", "list_routines"}
)
"""The tools `authoring_toolset` offers, which a routine's firing never sees."""

__all__ = ["ROUTINE_AUTHORING_TOOLS"]
