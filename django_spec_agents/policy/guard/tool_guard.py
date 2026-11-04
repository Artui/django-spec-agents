"""``ToolGuard`` — flip destructive tools to require human-in-the-loop approval."""

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
    """Gates destructive server-side tools behind the AG-UI approval loop.

    pydantic-ai supplies the mechanism — a tool whose definition is
    ``kind="unapproved"`` defers to an interrupt the client approves or denies —
    and this supplies the policy, flipping a plain ``function`` tool to
    ``unapproved`` at ``prepare_tools`` time when it is destructive. Server-side
    tools thereby get the confirmation gate the web component already applies to
    client-registered ones.

    Destructiveness is read from every vocabulary a toolset might declare it in,
    so one hook covers every tool the agent sees wherever it came from:

    - **Registry** ``@tool(destructive=True)``, collected at construction: the
      flag lives on the spec and never reaches pydantic-ai, which sees a bare
      callable, so the capability reads it directly.
    - **Bridged MCP tools**, through the
      [`DESTRUCTIVE_METADATA_KEY`][django_spec_agents.types.destructive_metadata_key.DESTRUCTIVE_METADATA_KEY] the
      bridge stamps into ``ToolDefinition.metadata``. The bridge marks a tool
      unless its ``readOnlyHint`` is true or its ``destructiveHint`` is false, as
      MCP reads the pair, so a mutation registered with
      ``annotations={"destructiveHint": False}`` is not gated. A tool listed
      with no ``annotations`` is not marked.
    - **MCP tool annotations** — ``metadata["annotations"]``, read by the bridge's
      rule: annotations that say something mark the tool unless ``readOnlyHint``
      is true or ``destructiveHint`` is false, and a field that is ``None`` is
      unsaid, as are annotations with nothing said. A toolset that speaks MCP's
      own vocabulary rather than this package's key declares a mutation this
      way, ``SpecToolset`` over a ``ServiceSpec`` and a remote MCP server among
      them. Reading it gates a spec attached in process the way the bridge's key
      gates the same spec over MCP, so a transport swap cannot silently
      remove the gate.
    - **The ``x-destructive`` schema stamp** at the root of
      ``parameters_json_schema``, which is what
      [`build_input_schema`][django_spec_agents.build_input_schema] writes,
      so a project deriving a schema with the package's own helper and
      attaching the tool through ``toolsets=`` is gated by it too.
    - **Project overrides**: ``require_approval`` force-gates a name, ``exempt``
      un-gates one, and ``exempt`` wins.

    A hint has to *say* something. Annotations that say nothing, an absent stamp
    or metadata of another shape entirely leave the tool alone — silence is not
    a claim, and ``require_approval`` is the answer for a tool whose source
    declares nothing. Annotations that say anything are read by MCP's defaults,
    as the bridge reads them.

    Only ``kind="function"`` tools are flipped — an ``external`` tool is already
    gated client-side, and an ``output`` tool is not executed.

    The guard touches only ``prepare_tools``, so it is orthogonal to
    [`AuditCapability`][django_spec_agents.AuditCapability]
    and audit still records the tool when an approved call finally runs.
    """

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
    """Whether the tool itself says it mutates, in any of the three vocabularies.

    Read in cost order, cheapest first. Each is checked independently: a toolset
    speaks one of them, and which one is a fact about its author rather than
    about the tool.
    """
    metadata = tool_def.metadata or {}
    if metadata.get(DESTRUCTIVE_METADATA_KEY):
        return True
    # MCP's own name for the same fact: ``SpecToolset`` writes a ``readOnlyHint``
    # and nothing else, and a remote client (pydantic-ai's ``MCPServer``) dumps
    # the server's whole ``ToolAnnotations`` with the fields it never sent as
    # ``None``. ``Mapping`` guard because metadata is free-form.
    annotations = metadata.get("annotations")
    if isinstance(annotations, Mapping) and _annotations_declare_a_mutation(annotations):
        return True
    return bool((tool_def.parameters_json_schema or {}).get(X_DESTRUCTIVE_KEY))


def _annotations_declare_a_mutation(annotations: Mapping[str, Any]) -> bool:
    """Whether MCP annotations mark a tool destructive, as the bridge reads them.

    MCP defaults ``readOnlyHint`` to false and, on a tool that is not read-only,
    ``destructiveHint`` to true, so annotations that say anything at all mark a
    tool unless ``readOnlyHint`` is true or ``destructiveHint`` is false. A
    ``None`` is a field the server never sent, so it is unsaid, and annotations
    with nothing said (``{}``, or every field ``None``, which is what a server's
    empty annotations object dumps to) are no claim: otherwise that tool would
    be gated though its server claimed nothing.
    """
    if all(value is None for value in annotations.values()):
        return False
    return (
        annotations.get("readOnlyHint") is not True
        and annotations.get("destructiveHint") is not False
    )


__all__ = ["ToolGuard"]
