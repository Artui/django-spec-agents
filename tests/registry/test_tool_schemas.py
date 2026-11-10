from __future__ import annotations

import dataclasses
import difflib
import enum
import json
import os
from pathlib import Path
from typing import Annotated, Any, Optional

import annotated_types
import pytest
from pydantic import BaseModel, Field
from pydantic_ai import RunContext

from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.registry.decorator import tool
from django_spec_agents.registry.tool_registry import ToolRegistry
from django_spec_agents.types.schema_strategy import SchemaStrategy
from django_spec_agents.types.tool_category import ToolCategory

DUMP = Path(__file__).parent / "tool_schemas.json"
WRITE = os.environ.get("TOOL_SCHEMAS_WRITE") == "1"
# The callable is not data, and the schema is dumped beside the spec, not in it.
SPEC_SKIPPED = ("fn",)


class _Colour(enum.Enum):
    RED = "red"
    BLUE = "blue"


class _Address(BaseModel):
    street: str
    zip_code: Annotated[str, Field(pattern=r"^\d{5}$")]


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    @tool(registry)
    def scalars(name: str, count: int, ratio: float, flag: bool, anything: Any, bare) -> None:
        """Take one parameter of each scalar type."""

    @tool(registry)
    def defaults(limit: int = 10, *, label: str = "all", verbose: bool = False) -> None:
        """Take parameters with plain defaults."""

    @tool(registry)
    def unions(
        note: str | None,
        legacy: Optional[int] = None,  # noqa: UP045
        either: int | str = 0,
    ) -> None:
        """Take optional and union parameters."""

    @tool(registry)
    def annotated(
        page: Annotated[int, Field(description="1-based page.", ge=1, le=500)],
        query: Annotated[
            str,
            Field(
                title="Search text",
                description="Text to look for.",
                examples=["red", "blue"],
                min_length=1,
                max_length=64,
                pattern=r"^\w+$",
            ),
        ],
        score: Annotated[float, annotated_types.Gt(0), annotated_types.Lt(1)],
        tags: Annotated[list[str], Field(min_length=1, max_length=5)],
        step: Annotated[int, annotated_types.MultipleOf(5)] = 5,
    ) -> None:
        """Take annotated parameters."""

    @tool(registry)
    def field_defaults(
        size: int = Field(default=20, ge=1, le=100, description="Page size."),
        cursor: str = Field(default="", description="Opaque cursor."),
        owner: str = Field(description="Owner handle."),
    ) -> None:
        """Take parameters given as a Field."""

    @tool(registry)
    def containers(
        ids: list[int],
        rows: list[Annotated[str, Field(max_length=8)]],
        extra: dict[str, Any],
        unique: set[str],
        pairs: tuple[int, str],
        nested: list[Annotated[_Address, Field(description="An address.")]],
    ) -> None:
        """Take container parameters."""

    @tool(registry)
    def unmapped(colour: _Colour, address: _Address | None = None) -> None:
        """Take types the hand-built schema leaves unconstrained."""

    @tool(registry)
    def with_context(ctx: RunContext[AgentDeps], target: str) -> str:
        """Read the acting user through the run context."""
        return target

    @tool(registry, name="fetch_remote", description="Fetch a remote resource.")
    async def _fetch(url: str) -> str:
        """Not the description."""
        return url

    # No docstring: the description is empty.
    @tool(registry)
    def undocumented(value: int) -> int:
        return value

    @tool(
        registry,
        destructive=True,
        category=ToolCategory.UI_WRITE,
        confirm="Delete this record?",
        summary="Delete a record",
    )
    def delete_record(record_id: int) -> None:
        """Delete a record."""

    @tool(registry, category=ToolCategory.INTROSPECT)
    def introspect() -> dict[str, Any]:
        """List what the host exposes."""
        return {}

    @tool(registry, schema_strategy=SchemaStrategy.PYDANTIC, summary="Register a visit")
    def pydantic_derived(
        address: _Address,
        colour: _Colour = _Colour.RED,
        note: str | None = None,
        count: Annotated[int, Field(ge=0, description="Visitors.")] = 1,
    ) -> None:
        """Take parameters the pydantic strategy derives."""

    return registry


def _registry_with_pydantic_default() -> ToolRegistry:
    """A registry whose strategy is pydantic, so a tool takes it unprompted."""
    registry = ToolRegistry(schema_strategy=SchemaStrategy.PYDANTIC)

    @tool(registry, destructive=True, confirm="Merge these records?")
    def merge(
        keep: Annotated[int, Field(description="The record to keep.")],
        drop: list[int],
        reason: str | None = None,
    ) -> None:
        """Merge records."""

    @tool(registry, schema_strategy=SchemaStrategy.HAND_BUILT)
    def hand_built(reason: str | None = None) -> None:
        """Take a parameter the hand-built schema derives."""

    return registry


def _project(registry: ToolRegistry) -> list[dict[str, Any]]:
    """Every binding, in registration order: its spec's fields and its schema."""
    entries = []
    for binding in registry:
        entry = {
            field.name: getattr(binding.spec, field.name)
            for field in dataclasses.fields(binding.spec)
            if field.name not in SPEC_SKIPPED
        }
        entry["input_schema"] = binding.input_schema
        entries.append(entry)
    return entries


def _definitions() -> dict[str, Any]:
    """The two registries' bindings, each as the fields above."""
    return {
        "default_hand_built": _project(_registry()),
        "default_pydantic": _project(_registry_with_pydantic_default()),
    }


def _encode(definitions: dict[str, Any]) -> str:
    """The dump's text: sorted keys, two-space indent, one trailing newline."""
    return (
        json.dumps(definitions, indent=2, sort_keys=True, ensure_ascii=False, default=_enum_value)
        + "\n"
    )


def _enum_value(value: Any) -> Any:
    """A spec's category or strategy is an enum; the dump holds its value."""
    if isinstance(value, enum.Enum):
        return value.value
    raise TypeError(f"{type(value).__name__} is not part of the dump's encoding")


def test_the_registry_tool_schemas_match_the_checked_in_dump():
    current = _encode(_definitions())
    if WRITE:
        DUMP.write_text(current, encoding="utf-8")
        # A skip, not a pass: a left-behind write must not turn drift into a silent rewrite.
        pytest.skip("wrote tests/registry/tool_schemas.json")
    checked_in = DUMP.read_text(encoding="utf-8") if DUMP.exists() else ""
    if current != checked_in:
        diff = "".join(
            difflib.unified_diff(
                checked_in.splitlines(keepends=True),
                current.splitlines(keepends=True),
                fromfile="tests/registry/tool_schemas.json (checked in)",
                tofile="ToolRegistry (now)",
            )
        )
        lines = diff.splitlines(keepends=True)
        shown = "".join(lines[:200]) + (
            f"... {len(lines) - 200} more lines\n" if len(lines) > 200 else ""
        )
        raise AssertionError(
            "The registry's tool schemas changed. If the change is intended, run "
            "`make tool-schemas` and commit tests/registry/tool_schemas.json in the "
            f"same pull request.\n{shown}"
        )
