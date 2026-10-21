"""The subclass contract the reference page documents, held by a subclass.

A project scoping an assistant to what one run may reach narrows the catalog
in ``get_tools`` and wraps dispatch in ``call_tool``, both delegating to
``super()``, and reads ``specs`` and ``id`` at configuration time. Each is
asserted here through a real run loop, so a refactor moving dispatch off
``call_tool`` or the catalog off ``get_tools`` fails here rather than in a
consumer's assistant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from rest_framework.permissions import AllowAny
from rest_framework_services import ServiceSpec

from django_spec_agents.spec_tools import SpecToolset
from django_spec_agents.testing import tool_calling_model


def ping(user):
    """Ping."""
    return {"ok": True}


def pong(user):
    """Pong."""
    return {"ok": False}


@dataclass
class _ScopedDeps:
    user: Any
    allowed: frozenset[str] = field(default_factory=frozenset)


class _Scoped(SpecToolset):
    """Offers a run only the tools its deps allow, and records each call."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    async def get_tools(self, ctx):
        tools = await super().get_tools(ctx)
        return {name: tool for name, tool in tools.items() if name in ctx.deps.allowed}

    async def call_tool(self, name, tool_args, ctx, tool):
        self.calls.append(name)
        return await super().call_tool(name, tool_args, ctx, tool)


def _toolset() -> _Scoped:
    return _Scoped(
        {
            name: ServiceSpec(service=service, permission_classes=[AllowAny], atomic=False)
            for name, service in (("ping", ping), ("pong", pong))
        },
        id="scoped",
    )


async def test_a_run_is_offered_what_get_tools_returns_and_dispatches_through_call_tool():
    toolset = _toolset()
    offered: list[list[str]] = []
    calls_ping = tool_calling_model("ping").function

    def model(messages, info):
        offered.append(sorted(tool.name for tool in info.function_tools))
        return calls_ping(messages, info)

    agent = Agent(FunctionModel(model), deps_type=_ScopedDeps, toolsets=[toolset])

    result = await agent.run("go", deps=_ScopedDeps(user=None, allowed=frozenset({"ping"})))

    assert result.output == "done"
    assert toolset.calls == ["ping"]
    assert offered[0] == ["ping"]


def test_specs_and_id_answer_at_configuration_time_whatever_a_run_is_offered():
    toolset = _toolset()

    assert list(toolset.specs) == ["ping", "pong"]
    assert toolset.id == "scoped"
