from __future__ import annotations

from typing import Any

import django_spec_mcp
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django_service_specs import TRANSPORT_KEY, Parameters, ServiceSpec, Unrestricted
from django_spec_mcp import MCPServer

from django_spec_agents.integrations.mcp import MCPToolset

_SEEN: list[Any] = []


def _record(call_context: dict[str, Any]) -> dict[str, bool]:
    _SEEN.append(call_context.get(TRANSPORT_KEY))
    return {"ok": True}


_server = MCPServer(name="transport")
_server.register_tool(
    ServiceSpec(service=_record, reads=Parameters.of(), permissions=[Unrestricted()], atomic=False),
    name="record",
    description="Record the transport the call carried.",
)


async def test_a_bridged_calls_transport_is_whatever_django_spec_mcp_stamps():
    request = RequestFactory().post("/agent/")
    request.user = AnonymousUser()  # type: ignore[attr-defined]
    _SEEN.clear()

    await MCPToolset(_server, request).call_tool("record", {}, None, None)  # ty: ignore[invalid-argument-type]

    assert ["mcp" if hasattr(django_spec_mcp, "MCP_TRANSPORT") else None] == _SEEN
