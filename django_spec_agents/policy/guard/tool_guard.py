from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import ToolDefinition

from django_spec_agents.policy.guard.types.tool_guard_config import ToolGuardConfig
from django_spec_agents.registry.tool_registry import ToolRegistry
from django_spec_agents.types.destructive_metadata_key import DESTRUCTIVE_METADATA_KEY
from django_spec_agents.types.x_destructive_key import X_DESTRUCTIVE_KEY


class ToolGuard(AbstractCapability[Any]):
    """Gates destructive server-side tools behind the AG-UI approval loop."""

    def __init__(self, registry: ToolRegistry, *, config: ToolGuardConfig) -> None:
        self._destructive_names = frozenset(
            binding.spec.name for binding in registry if binding.spec.destructive
        )
        self._exempt = config.exempt
        self._require_approval = config.require_approval

    async def prepare_tools(
        self,
        ctx: RunContext[Any],
        tool_defs: list[ToolDefinition],
    ) -> list[ToolDefinition]:
        return [
            dataclasses.replace(tool_def, kind="unapproved")
            if tool_def.kind == "function" and self._requires_approval(tool_def)
            else tool_def
            for tool_def in tool_defs
        ]

    def _requires_approval(self, tool_def: ToolDefinition) -> bool:
        """Whether ``tool_def`` should be gated, applying the policy precedence."""
        if tool_def.name in self._exempt:
            return False
        if tool_def.name in self._require_approval:
            return True
        if tool_def.name in self._destructive_names:
            return True
        return _declares_a_mutation(tool_def)


def _declares_a_mutation(tool_def: ToolDefinition) -> bool:
    """Whether the tool itself says it mutates, in any of the three vocabularies."""
    metadata = tool_def.metadata or {}
    if metadata.get(DESTRUCTIVE_METADATA_KEY):
        return True
    annotations = metadata.get("annotations")
    if isinstance(annotations, Mapping) and _annotations_declare_a_mutation(annotations):
        return True
    return bool((tool_def.parameters_json_schema or {}).get(X_DESTRUCTIVE_KEY))


def _annotations_declare_a_mutation(annotations: Mapping[str, Any]) -> bool:
    """Whether MCP annotations mark a tool destructive, as the bridge reads them."""
    if all(value is None for value in annotations.values()):
        return False
    return (
        annotations.get("readOnlyHint") is not True
        and annotations.get("destructiveHint") is not False
    )


__all__ = ["ToolGuard"]
