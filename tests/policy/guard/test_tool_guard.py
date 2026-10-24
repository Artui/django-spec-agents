from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic_ai.tools import ToolDefinition

from django_spec_agents.policy.guard.tool_guard import ToolGuard
from django_spec_agents.policy.guard.types.tool_guard_config import ToolGuardConfig
from django_spec_agents.registry.build_input_schema import build_input_schema
from django_spec_agents.registry.decorator import tool
from django_spec_agents.registry.tool_registry import ToolRegistry
from django_spec_agents.types.destructive_metadata_key import DESTRUCTIVE_METADATA_KEY


def _registry() -> ToolRegistry:
    reg = ToolRegistry()

    @tool(reg, destructive=True)
    def delete_thing(target: str) -> str:
        """Delete a thing."""
        return f"deleted {target}"

    @tool(reg)
    def read_thing(target: str) -> str:
        """Read a thing (safe)."""
        return f"read {target}"

    return reg


def _def(
    name: str, *, kind: str = "function", metadata: dict[str, Any] | None = None
) -> ToolDefinition:
    return ToolDefinition(
        name=name, parameters_json_schema={"type": "object"}, kind=kind, metadata=metadata
    )


async def _prepare(guard: ToolGuard, defs: list[ToolDefinition]) -> dict[str, str]:
    # ``prepare_tools`` ignores ``ctx``; pass ``None`` (tests aren't type-checked).
    prepared = await guard.prepare_tools(None, defs)  # type: ignore[arg-type]
    return {d.name: d.kind for d in prepared}


_ENABLED = ToolGuardConfig(enabled=True)

# Each row: the guard's registry, its config, the tool definitions it sees and
# the kind each comes out as.
_KINDS = [
    pytest.param(
        _registry,
        _ENABLED,
        [_def("delete_thing"), _def("read_thing")],
        {"delete_thing": "unapproved", "read_thing": "function"},
        id="destructive-registry-tool",
    ),
    # A drf-mcp bridged mutating tool carries destructiveness in metadata, not in
    # the registry; the guard must gate it just the same.
    pytest.param(
        ToolRegistry,
        _ENABLED,
        [
            _def("mcp_delete", metadata={DESTRUCTIVE_METADATA_KEY: True}),
            _def("mcp_list", metadata={DESTRUCTIVE_METADATA_KEY: False}),
            _def("mcp_read"),
        ],
        {"mcp_delete": "unapproved", "mcp_list": "function", "mcp_read": "function"},
        id="metadata-marked-tool",
    ),
    pytest.param(
        _registry,
        ToolGuardConfig(enabled=True, exempt=frozenset({"delete_thing"})),
        [_def("delete_thing")],
        {"delete_thing": "function"},
        id="exempt-wins-over-destructive",
    ),
    pytest.param(
        _registry,
        ToolGuardConfig(enabled=True, require_approval=frozenset({"read_thing"})),
        [_def("read_thing")],
        {"read_thing": "unapproved"},
        id="require-approval-forces-a-non-destructive-tool",
    ),
    pytest.param(
        ToolRegistry,
        ToolGuardConfig(
            enabled=True, exempt=frozenset({"foo"}), require_approval=frozenset({"foo"})
        ),
        [_def("foo")],
        {"foo": "function"},
        id="exempt-wins-over-require-approval",
    ),
    # An external (frontend) tool is already gated client-side; an output tool is
    # not executed. Only ``function`` tools are flipped, even when destructive.
    pytest.param(
        _registry,
        ToolGuardConfig(enabled=True, require_approval=frozenset({"ext", "out"})),
        [
            _def("delete_thing", kind="external"),
            _def("ext", kind="external"),
            _def("out", kind="output"),
        ],
        {"delete_thing": "external", "ext": "external", "out": "output"},
        id="external-and-output-tools-are-left-alone",
    ),
]


@pytest.mark.parametrize(("registry", "config", "defs", "expected"), _KINDS)
async def test_the_guard_flips_only_the_tools_it_gates(
    registry: Callable[[], ToolRegistry],
    config: ToolGuardConfig,
    defs: list[ToolDefinition],
    expected: dict[str, str],
) -> None:
    assert await _prepare(ToolGuard(registry(), config=config), defs) == expected


class TestEveryDestructivenessSignal:
    """The docstring promises one hook covers every tool the agent sees wherever
    it came from. Each test below is a signal that reached the guard as a
    ``function`` tool and ran unconfirmed."""

    async def test_flips_a_tool_whose_mcp_annotation_says_it_mutates(self) -> None:
        """A toolset that speaks MCP's own vocabulary nests ``readOnlyHint``
        under ``annotations`` instead of stamping the metadata key.

        The same spec was therefore gated when it arrived over the drf-mcp
        bridge (which maps the hint onto the key) and ungated when the identical
        spec was attached in process.
        """
        guard = ToolGuard(ToolRegistry(), config=ToolGuardConfig(enabled=True))
        kinds = await _prepare(
            guard,
            [
                _def("create_order", metadata={"annotations": {"readOnlyHint": False}}),
                _def("list_orders", metadata={"annotations": {"readOnlyHint": True}}),
                _def("unhinted", metadata={"annotations": {}}),
                _def("not_a_mapping", metadata={"annotations": "nonsense"}),
            ],
        )
        assert kinds["create_order"] == "unapproved"
        assert kinds["list_orders"] == "function"
        assert kinds["unhinted"] == "function"
        assert kinds["not_a_mapping"] == "function"

    async def test_flips_a_tool_whose_schema_carries_the_destructive_stamp(self) -> None:
        """``build_input_schema`` is a public export, so a project can derive a
        schema outside the registration flow and attach the tool through
        ``toolsets=``. Its ``x-destructive`` stamp had no server-side reader at
        all: the gate saw a plain ``function`` tool and let it run."""

        def purge(target: str) -> str:
            """Purge a thing."""
            return target

        guard = ToolGuard(ToolRegistry(), config=ToolGuardConfig(enabled=True))
        kinds = await _prepare(
            guard,
            [
                ToolDefinition(
                    name="purge",
                    parameters_json_schema=build_input_schema(purge, destructive=True),
                ),
                ToolDefinition(
                    name="peek",
                    parameters_json_schema=build_input_schema(purge),
                ),
            ],
        )
        assert kinds["purge"] == "unapproved"
        assert kinds["peek"] == "function"

    async def test_exempt_still_wins_over_every_signal(self) -> None:
        guard = ToolGuard(
            ToolRegistry(),
            config=ToolGuardConfig(enabled=True, exempt=frozenset({"create_order"})),
        )
        kinds = await _prepare(
            guard,
            [_def("create_order", metadata={"annotations": {"readOnlyHint": False}})],
        )
        assert kinds["create_order"] == "function"


async def test_gates_the_same_service_spec_a_spec_toolset_exposes() -> None:
    """The reproduction, end to end: a mutating spec attached in process.

    ``SpecToolset`` builds its tool definitions from the spec kind — a
    ``ServiceSpec`` mutates, a ``SelectorSpec`` reads — and says so with MCP's
    ``readOnlyHint``. Serving a write in process rather than over the drf-mcp
    bridge is presented as a transport swap, so the gate has to survive it.
    """
    from django_service_specs import SelectorKind, SelectorSpec, ServiceSpec, Unrestricted

    from django_spec_agents.spec_tools import SpecToolset

    def create_order(user: Any) -> dict[str, Any]:
        """Create an order."""
        return {"ok": True}

    def list_orders(user: Any) -> list[Any]:
        """List orders."""
        return []

    toolset = SpecToolset(
        {
            "create_order": ServiceSpec(
                service=create_order, atomic=False, permissions=[Unrestricted()]
            ),
            "list_orders": SelectorSpec(
                kind=SelectorKind.LIST, selector=list_orders, permissions=[Unrestricted()]
            ),
        }
    )
    # ``get_tools`` ignores its ``RunContext``; there is no run here.
    tool_defs = [t.tool_def for t in (await toolset.get_tools(None)).values()]  # type: ignore[arg-type]

    guard = ToolGuard(ToolRegistry(), config=ToolGuardConfig(enabled=True))
    kinds = await _prepare(guard, tool_defs)

    assert kinds["create_order"] == "unapproved"
    assert kinds["list_orders"] == "function"
