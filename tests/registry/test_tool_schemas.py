"""The registry's tool schemas, pinned as the registry derives them.

A deployed agent's model reads what the host's own ``ToolRegistry`` holds for
each ``@tool``: its name, its description and the JSON Schema
``build_input_schema`` derives from the signature, stamped with the
``x-destructive`` / ``x-category`` / ``x-confirm`` / ``x-summary`` annotations a
client reads. A change to any of them is a change to an API whose callers are
not in this repository, so the bindings are checked in at
``tests/registry/tool_schemas.json`` and this test diffs them. It is the second
producer beside ``tests/spec_tools/tool_schemas.json``, which pins what
``SpecToolset.get_tools`` advertises; each has its own dump.

The dump is what the registry holds, read off each ``ToolBinding``. Nothing
here builds an entry by hand, so the file moves when the registry's output does,
and only then. It keeps every field of the binding's ``ToolSpec`` except the
callable (so a field the spec gains moves the dump) and the derived
``input_schema``.

The registry below is built for the dump: one tool per schema feature, not one
per test. Adding a feature to the derivation earns a tool here. It covers
scalar parameters, ``Annotated`` metadata (a ``Field``'s description, title and
examples, bounds, a pattern and length bounds), a ``Field`` given as the
default, plain defaults, an ``X | None`` union, containers, an unresolvable
type, a leading ``RunContext`` left out, the four ``x-`` annotations, and the
pydantic strategy, by the registry's default and by one tool's override.

A schema change regenerates the dump in the same pull request::

    make tool-schemas

which runs this test with ``TOOL_SCHEMAS_WRITE=1`` set, so it writes the file
instead of comparing, then skips so a run that wrote never reports a pass; the
diff of ``tool_schemas.json`` is then the change a reviewer reads.
"""

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

    # Scalars and a bare annotation: required, every JSON type, and an
    # unannotated parameter, which imposes no type.
    @tool(registry)
    def scalars(name: str, count: int, ratio: float, flag: bool, anything: Any, bare) -> None:
        """Take one parameter of each scalar type."""

    # A plain default leaves a parameter out of ``required``; a keyword-only
    # one does the same.
    @tool(registry)
    def defaults(limit: int = 10, *, label: str = "all", verbose: bool = False) -> None:
        """Take parameters with plain defaults."""

    # ``X | None`` as nullable, ``Optional`` and a union of more than one type.
    @tool(registry)
    def unions(
        note: str | None,
        legacy: Optional[int] = None,  # noqa: UP045
        either: int | str = 0,
    ) -> None:
        """Take optional and union parameters."""

    # ``Annotated`` metadata: a Field's description, title and examples, its
    # bounds, a pattern and length bounds, and ``annotated_types`` bounds.
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

    # A ``Field`` given as the default: its default is shown, its description
    # and bounds are read, and one without a default is still required.
    @tool(registry)
    def field_defaults(
        size: int = Field(default=20, ge=1, le=100, description="Page size."),
        cursor: str = Field(default="", description="Opaque cursor."),
        owner: str = Field(description="Owner handle."),
    ) -> None:
        """Take parameters given as a Field."""

    # Containers: a list of a type and of an annotated type, a dict, a set, a
    # tuple, and a list of an annotated model, whose item keeps only its
    # metadata.
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

    # A type with no hand-built mapping stays an empty fragment, wire-valid.
    @tool(registry)
    def unmapped(colour: _Colour, address: _Address | None = None) -> None:
        """Take types the hand-built schema leaves unconstrained."""

    # The run context leads and is not an argument.
    @tool(registry)
    def with_context(ctx: RunContext[AgentDeps], target: str) -> str:
        """Read the acting user through the run context."""
        return target

    # An async tool, a name and a description given outright rather than derived
    # from the function and its docstring.
    @tool(registry, name="fetch_remote", description="Fetch a remote resource.")
    async def _fetch(url: str) -> str:
        """Not the description."""
        return url

    # No docstring: the description is empty.
    @tool(registry)
    def undocumented(value: int) -> int:
        return value

    # The four ``x-`` annotations, with a non-default category.
    @tool(
        registry,
        destructive=True,
        category=ToolCategory.UI_WRITE,
        confirm="Delete this record?",
        summary="Delete a record",
    )
    def delete_record(record_id: int) -> None:
        """Delete a record."""

    # A category alone: ``x-category`` is stamped on every schema, the other
    # three only when set.
    @tool(registry, category=ToolCategory.INTROSPECT)
    def introspect() -> dict[str, Any]:
        """List what the host exposes."""
        return {}

    # The pydantic strategy for one tool, over the same parameters: titles,
    # ``anyOf`` for an optional, defaults and ``$defs`` for a model and an enum.
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

    # And the hand-built mapping, by override, on a pydantic registry.
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
        # A skip, not a pass: a write left exported must not turn drift into a
        # silent rewrite that CI reports green.
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
