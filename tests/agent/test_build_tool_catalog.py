from __future__ import annotations

from typing import Any

import pytest
from django_service_specs import ServiceSpec, Unrestricted
from django_spec_mcp import MCPServer

from django_spec_agents import ToolRegistry, tool
from django_spec_agents.agent.build_tool_catalog import build_tool_catalog
from tests.agent.catalog_server import server as _DRF
from tests.integrations.drf_specs import SPECS as _SPECS


def _svc() -> dict[str, Any]:
    return {}


def _spec_mcp_server() -> MCPServer:
    """The three label cases of ``catalog_server``, on a django-spec-mcp server."""
    server = MCPServer(name="catalog-spec-mcp")
    spec = ServiceSpec(service=_svc, permissions=[Unrestricted()], atomic=False)
    server.register_tool(
        spec,
        name="ping",
        description="Ping it.",
        display_name="Ping the service",
        display_description="Health check.",
    )
    server.register_tool(
        spec, name="lookup_widget", title="Lookup widget", description="Find a widget."
    )
    server.register_tool(spec, name="raw_tool", description="")
    return server


_SPEC_MCP = _spec_mcp_server()

# The labels both servers' fixtures resolve to, in registration order.
_SERVER_ENTRIES = [
    {"name": "ping", "summary": "Ping the service", "description": "Health check."},
    {"name": "lookup_widget", "summary": "Lookup widget", "description": "Find a widget."},
    {"name": "raw_tool", "summary": "Raw tool"},
]


def _registry() -> ToolRegistry:
    reg = ToolRegistry()

    @tool(reg, summary="Look up a user")
    def find_user(email: str) -> dict[str, Any]:
        """Find a user by email."""
        return {}

    @tool(reg)
    def list_open_orders() -> list[Any]:
        """List the open orders."""
        return []

    return reg


def test_registry_tools_use_summary_or_prettified_name_with_description() -> None:
    by_name = {e["name"]: e for e in build_tool_catalog(_registry())}
    # Explicit @tool(summary=) wins; description comes from the docstring.
    assert by_name["find_user"]["summary"] == "Look up a user"
    assert by_name["find_user"]["description"] == "Find a user by email."
    # No summary → prettified from the tool name.
    assert by_name["list_open_orders"]["summary"] == "List open orders"


def test_drf_mcp_tools_resolve_display_name_then_title_then_prettified() -> None:
    by_name = {
        e["name"]: e
        for e in build_tool_catalog(ToolRegistry(), mcp_server=_DRF, service_specs=_SPECS)
    }
    # display_name / display_description win.
    assert by_name["ping"]["summary"] == "Ping the service"
    assert by_name["ping"]["description"] == "Health check."
    # No display_*: summary ← title, description ← protocol description.
    assert by_name["lookup_widget"]["summary"] == "Lookup widget"
    assert by_name["lookup_widget"]["description"] == "Find a widget."
    # Nothing: summary prettified from the name, no description key.
    assert by_name["raw_tool"]["summary"] == "Raw tool"
    assert "description" not in by_name["raw_tool"]


def test_a_spec_mcp_servers_tools_resolve_their_labels() -> None:
    # django-spec-mcp's bindings carry the same four label fields as drf-mcp's.
    assert build_tool_catalog(ToolRegistry(), mcp_server=_SPEC_MCP) == _SERVER_ENTRIES


@pytest.mark.parametrize("server", [_SPEC_MCP, _DRF], ids=["spec-mcp", "drf-mcp"])
def test_the_old_keyword_still_lists_the_servers_tools(server: Any) -> None:
    # Deprecated for a minor: the same catalog, and a warning naming the new one.
    with pytest.warns(DeprecationWarning, match=r"pass the server as mcp_server=\.$"):
        entries = build_tool_catalog(ToolRegistry(), drf_mcp_server=server)
    assert entries == _SERVER_ENTRIES


def test_both_server_keywords_are_refused() -> None:
    with pytest.raises(TypeError, match="takes mcp_server or drf_mcp_server, not both"):
        build_tool_catalog(ToolRegistry(), mcp_server=_SPEC_MCP, drf_mcp_server=_DRF)


def test_service_specs_appear_in_catalog_with_prettified_summary() -> None:
    # Specs only — no MCP server, which also defines ``ping`` and would win.
    by_name = {e["name"]: e for e in build_tool_catalog(ToolRegistry(), service_specs=_SPECS)}
    # No x-summary on a spec → prettified name; description ← the service docstring.
    assert by_name["ping"]["summary"] == "Ping"
    assert by_name["ping"]["description"] == "Ping the server."


@pytest.mark.parametrize(
    "sources",
    [
        {"mcp_server": _SPEC_MCP},
        {"mcp_server": _DRF},
        {"service_specs": _SPECS},
        {"mcp_server": _SPEC_MCP, "service_specs": _SPECS},
    ],
    ids=["spec-mcp", "drf-mcp", "service-spec", "both"],
)
def test_registry_wins_on_name_collision(sources: dict[str, Any]) -> None:
    reg = ToolRegistry()

    @tool(reg, summary="Local ping")
    def ping() -> dict[str, Any]:
        """Local ping."""
        return {}

    entries = [e for e in build_tool_catalog(reg, **sources) if e["name"] == "ping"]
    assert entries == [{"name": "ping", "summary": "Local ping", "description": "Local ping."}]
