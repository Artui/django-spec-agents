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
    # An MCP-bridged mutating tool carries destructiveness in metadata, not in
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

        The same spec was therefore gated when it arrived over an MCP bridge
        (which maps the hint onto the key) and ungated when the identical spec
        was attached in process.
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


# Each row: the annotations a server sent (only the fields it set) and whether a
# tool carrying them is destructive. The bridge's rule is the reference: a
# tool that says something is marked unless ``readOnlyHint`` is true or
# ``destructiveHint`` is false; one that says nothing is not.
_ANNOTATION_TRUTH_TABLE = [
    pytest.param({}, False, id="empty-says-nothing"),
    pytest.param({"readOnlyHint": True}, False, id="read-only"),
    pytest.param(
        {"readOnlyHint": True, "destructiveHint": True}, False, id="read-only-beats-destructive"
    ),
    pytest.param({"readOnlyHint": False}, True, id="mutation"),
    pytest.param({"readOnlyHint": False, "destructiveHint": True}, True, id="destructive-mutation"),
    pytest.param(
        {"readOnlyHint": False, "destructiveHint": False}, False, id="non-destructive-mutation"
    ),
    pytest.param({"destructiveHint": True}, True, id="destructive-read-only-unsaid"),
    pytest.param({"destructiveHint": False}, False, id="non-destructive-read-only-unsaid"),
    pytest.param({"title": "T"}, True, id="title-only-leaves-both-unsaid"),
    pytest.param({"idempotentHint": True}, True, id="other-hint-leaves-both-unsaid"),
    pytest.param({"readOnlyHint": True, "title": "T"}, False, id="read-only-with-title"),
]

# What pydantic-ai's ``MCPServer`` stores for the same annotations:
# ``ToolAnnotations.model_dump(by_alias=True)``, which lists every field and gives
# the ones the server never sent as ``None``.
_FULL_DUMP = {
    "title": None,
    "readOnlyHint": None,
    "destructiveHint": None,
    "idempotentHint": None,
    "openWorldHint": None,
}


def _full_dump(sent: dict[str, Any]) -> dict[str, Any]:
    return {**_FULL_DUMP, **sent}


class TestMcpAnnotationsAgreeWithTheBridge:
    @pytest.mark.parametrize(("sent", "destructive"), _ANNOTATION_TRUTH_TABLE)
    @pytest.mark.parametrize("shape", [dict, _full_dump], ids=["plain-dict", "full-dump"])
    async def test_the_guard_reads_annotations_as_the_bridge_does(
        self, sent: dict[str, Any], destructive: bool, shape: Callable[[dict[str, Any]], Any]
    ) -> None:
        guard = ToolGuard(ToolRegistry(), config=ToolGuardConfig(enabled=True))
        kinds = await _prepare(guard, [_def("t", metadata={"annotations": shape(sent)})])
        assert kinds["t"] == ("unapproved" if destructive else "function")

    @pytest.mark.parametrize(("sent", "destructive"), _ANNOTATION_TRUTH_TABLE)
    async def test_the_table_is_what_the_bridge_marks(
        self, sent: dict[str, Any], destructive: bool
    ) -> None:
        """The expected column is not the guard's own reading: it is what the
        bridge stamps for the same annotations on the wire."""
        from types import SimpleNamespace

        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from django_spec_agents.integrations.mcp import MCPToolset

        request = RequestFactory().post("/agent/")
        request.user = AnonymousUser()  # type: ignore[attr-defined]
        tool_listing = {"name": "t", "inputSchema": {"type": "object"}, "annotations": sent}

        async def no_unavailable_tools(**_kw: Any) -> dict[str, Any]:
            return {}

        server = SimpleNamespace(
            list_tools=lambda *_a, **_k: {"tools": [tool_listing]},
            aunavailable_tools=no_unavailable_tools,
        )
        toolset = MCPToolset(server, request)  # type: ignore[arg-type]
        listed = await toolset.get_tools(None)  # type: ignore[arg-type]
        marked = listed["t"].tool_def.metadata == {DESTRUCTIVE_METADATA_KEY: True}
        assert marked is destructive


async def test_gates_the_same_service_spec_a_spec_toolset_exposes() -> None:
    """The reproduction, end to end: a mutating spec attached in process.

    ``SpecToolset`` builds its tool definitions from the spec kind — a
    ``ServiceSpec`` mutates, a ``SelectorSpec`` reads — and says so with MCP's
    ``readOnlyHint``. Serving a write in process rather than over the MCP
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
