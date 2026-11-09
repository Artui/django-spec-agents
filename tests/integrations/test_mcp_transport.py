"""What transport a call through the ``[mcp]`` bridge reads.

The bridge stamps nothing of its own: the context a bridged call carries is built
by django-spec-mcp, so the transport is whatever that release stamps. Release 0.9.0,
this package's floor, stamps none, and a release naming ``MCP_TRANSPORT`` stamps
``"mcp"``, the literal the bridge's guide names. Which half runs depends on the installed
release: the lock holds the newest, the ``lowest declared versions`` job the floor.
"""

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
