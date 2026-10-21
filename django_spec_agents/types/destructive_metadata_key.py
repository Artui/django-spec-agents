"""The "this tool mutates" signal on ``ToolDefinition.metadata``.

The same signal as ``x-destructive``, but on metadata rather than the schema,
because metadata is the channel a *bridge* controls where the schema belongs to
the tool's author: ``ToolGuard`` reads it at ``prepare_tools`` time for tools
whose destructiveness the ``@tool`` registry does not know, such as a drf-mcp
tool whose ``readOnlyHint`` the bridge maps onto this key. It lives in process
only and is never stored, so its value follows the package's name.
"""

from __future__ import annotations

DESTRUCTIVE_METADATA_KEY = "django_spec_agents.destructive"

__all__ = ["DESTRUCTIVE_METADATA_KEY"]
