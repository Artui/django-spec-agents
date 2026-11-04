from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django_spec_agents.types.schema_strategy import SchemaStrategy
from django_spec_agents.types.tool_category import ToolCategory


@dataclass(frozen=True)
class ToolSpec:
    """Canonical declaration of a server-side tool.

    Bundles the callable with the metadata the registry needs to expose it to a
    Pydantic-AI agent and to a frontend.
    """

    name: str
    """Stable identifier exposed to the agent, unique within a
    [`ToolRegistry`][django_spec_agents.ToolRegistry]."""

    fn: Callable[..., Any]
    """The callable implementing the tool. Its parameters must be typed; the
    registry derives a JSON Schema from the signature."""

    description: str
    """Summary shown to the agent. Most clients display the first line."""

    destructive: bool = False
    """Whether calling this tool may mutate state. Stamped as
    ``x-destructive``, and an enabled
    [`ToolGuard`][django_spec_agents.ToolGuard] gates it server-side."""

    category: ToolCategory = ToolCategory.OTHER
    """Coarse capability grouping, stamped as ``x-category``."""

    confirm: str | None = None
    """A confirmation prompt for a destructive tool, stamped as ``x-confirm``,
    shown instead of a generic "Run <tool>?"."""

    summary: str | None = None
    """A short label, stamped as ``x-summary``, shown on the tool-call card
    instead of the raw tool name."""

    schema_strategy: SchemaStrategy | None = None
    """How this tool's ``ToolBinding.input_schema`` is derived. ``None`` takes
    the registering [`ToolRegistry`][django_spec_agents.ToolRegistry]'s
    strategy; a value overrides it for this tool alone, so one registry can
    hold tools of both strategies."""


__all__ = ["ToolSpec"]
