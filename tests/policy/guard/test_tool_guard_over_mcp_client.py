from __future__ import annotations

from typing import Any

import httpx2
import pytest
from django.core.handlers.asgi import ASGIHandler
from django.urls import path
from django_service_specs import Parameter, Parameters, ServiceSpec, Unrestricted
from django_spec_mcp import MCPServer
from django_spec_mcp.auth.backends.allow_any_backend import AllowAnyBackend
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport
from pydantic_ai import Agent, DeferredToolRequests, RunContext
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from django_spec_agents.policy.guard.tool_guard import ToolGuard
from django_spec_agents.policy.guard.types.tool_guard_config import ToolGuardConfig
from django_spec_agents.registry.tool_registry import ToolRegistry


def _add_numbers(*, a: int, b: int) -> dict[str, Any]:
    """Add two numbers."""
    return {"result": a + b}


# One mutation per ``annotations=`` a project can register it with. ``None``
# leaves the server to derive them from the spec, which says the tool mutates.
# The server merges what is given over that derivation, so ``{}`` would list the
# derived hints again; nulling both hints is what makes a server send
# annotations that say nothing, ``{"readOnlyHint": null, "destructiveHint":
# null}``, which the client dumps with every field ``None``.
_SERVER = MCPServer(name="guarded", auth_backend=AllowAnyBackend())
for _name, _annotations in (
    ("plain", None),
    ("read_only", {"readOnlyHint": True}),
    ("non_destructive", {"destructiveHint": False}),
    ("destructive", {"destructiveHint": True}),
    ("unsaid", {"readOnlyHint": None, "destructiveHint": None}),
):
    _SERVER.register_tool(
        ServiceSpec(
            service=_add_numbers,
            reads=Parameters.of(
                Parameter("a", "integer", required=True), Parameter("b", "integer", required=True)
            ),
            permissions=[Unrestricted()],
            atomic=False,
        ),
        name=_name,
        description="Add two numbers.",
        annotations=_annotations,
    )

# This module is the URLconf the test points Django at.
urlpatterns = [path("mcp/", _SERVER.async_urls)]


def _asgi_client(**kwargs: Any) -> httpx2.AsyncClient:
    # FastMCP passes headers, auth and timeouts; only the transport changes.
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=ASGIHandler()), **kwargs)


@pytest.fixture
def mcp_toolset(settings: Any) -> MCPToolset[Any]:
    settings.ROOT_URLCONF = __name__
    transport = StreamableHttpTransport("http://testserver/mcp/", httpx_client_factory=_asgi_client)
    return MCPToolset(Client(transport))


async def test_the_guard_gates_what_the_remote_annotations_mark(
    mcp_toolset: MCPToolset[Any],
) -> None:
    guard = ToolGuard(ToolRegistry(), config=ToolGuardConfig(enabled=True))
    agent = Agent(
        TestModel(),
        toolsets=[mcp_toolset],
        capabilities=[guard],
        output_type=[str, DeferredToolRequests],
    )

    result = await agent.run("Add some numbers.")

    assert isinstance(result.output, DeferredToolRequests)
    # Tools that say they are safe run; the other two wait for a human.
    assert sorted(call.tool_name for call in result.output.approvals) == ["destructive", "plain"]


async def test_the_unsaid_tool_reaches_the_guard_saying_nothing(
    mcp_toolset: MCPToolset[Any],
) -> None:
    # Pins ``unsaid``'s branch: were it to say ``readOnlyHint: True``, it would run ungated.
    ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    async with mcp_toolset:
        tools = await mcp_toolset.get_tools(ctx)

    annotations = tools["unsaid"].tool_def.metadata["annotations"]
    assert annotations
    assert all(value is None for value in annotations.values())
