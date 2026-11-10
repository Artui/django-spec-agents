from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django_spec_agents.registry.types.tool_spec import ToolSpec


@dataclass(frozen=True)
class ToolBinding:
    """A registered tool plus the JSON Schema derived from its signature."""

    spec: ToolSpec
    input_schema: dict[str, Any]


__all__ = ["ToolBinding"]
