from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest
from django.test import RequestFactory
from django_service_specs import (
    Parameter,
    Parameters,
    PermissionCheck,
    ServiceSpec,
    Unrestricted,
)
from django_spec_mcp import JsonRpcError, JsonRpcErrorCode, MCPServer
from django_spec_mcp.registry.types.ui_tool_meta import UIToolMeta
from django_spec_mcp.types.ui_visibility import UIVisibility
from pydantic_ai import Agent
from pydantic_ai._mcp_apps import MCP_APP_METADATA_KEY, get_mcp_app_tool_result
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturn
from pydantic_ai.models.function import AgentInfo, FunctionModel

from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.integrations.mcp import MCPToolset
from tests.integrations.mcp_server import add_numbers
from tests.tool_retries import is_tool_answer, tool_answers

VIEW = "ui://apps/sum"
_TWO = Parameters.of(
    Parameter("a", "integer", required=True), Parameter("b", "integer", required=True)
)


class _Authenticated(PermissionCheck):
    def has_permission(self, principal: Any, spec: Any) -> bool:
        return bool(getattr(principal, "is_authenticated", False))


def _spec() -> ServiceSpec:
    return ServiceSpec(service=add_numbers, reads=_TWO, permissions=[Unrestricted()], atomic=False)


def _build() -> MCPServer:
    apps = MCPServer(name="apps")
    apps.register_ui_resource(name="sum_view", uri=VIEW, html="<p>sum</p>")
    for name, visibility in (
        ("both", ()),
        ("model_only", (UIVisibility.MODEL,)),
        ("app_only", (UIVisibility.APP,)),
        ("either", (UIVisibility.MODEL, UIVisibility.APP)),
    ):
        apps.register_tool(
            _spec(),
            name=name,
            description="Add two numbers.",
            ui=UIToolMeta(resource_uri=VIEW, visibility=visibility),
        )
    apps.register_tool(_spec(), name="plain", description="Add two numbers.")
    return apps


apps_server = _build()


def _request() -> HttpRequest:
    request = RequestFactory().post("/agent/")
    request.user = AnonymousUser()  # type: ignore[attr-defined]
    return request


async def _offered() -> set[str]:
    tools = await MCPToolset(apps_server, _request()).get_tools(None)  # type: ignore[arg-type]
    return set(tools)


async def test_a_tool_only_the_app_may_call_is_not_offered_to_the_model() -> None:
    assert await _offered() == {"both", "model_only", "either", "plain"}


async def test_the_tools_meta_is_kept_on_its_definition() -> None:
    tools = await MCPToolset(apps_server, _request()).get_tools(None)  # type: ignore[arg-type]
    assert tools["either"].tool_def.metadata["meta"]["ui"] == {  # type: ignore[index]
        "resourceUri": VIEW,
        "visibility": ["model", "app"],
    }
    assert (
        tools["plain"].tool_def.metadata is None or "meta" not in tools["plain"].tool_def.metadata
    )


async def test_the_retry_hint_names_only_what_the_model_may_call() -> None:
    toolset = MCPToolset(apps_server, _request())
    await toolset.get_tools(None)  # type: ignore[arg-type]
    assert toolset._advertised_names("missing") == ["both", "model_only", "either", "plain"]


async def _app_return(name: str, args: dict[str, Any]) -> Any:
    toolset = MCPToolset(apps_server, _request())
    tools = await toolset.get_tools(None)  # type: ignore[arg-type]
    return await toolset.call_tool(name, args, None, tools[name])


async def test_a_ui_linked_tool_returns_the_value_with_the_app_record() -> None:
    value = await _app_return("both", {"a": 1, "b": 2})
    assert isinstance(value, ToolReturn)
    assert value.return_value == {"result": 3}
    assert type(value.metadata) is dict
    record = value.metadata["mcp_app"]
    assert record["resource_uri"] == VIEW
    assert record["tool_input"] == {"a": 1, "b": 2}
    assert record["toolset_id"] == "mcp"
    assert record["result"]["structuredContent"] == {"result": 3}
    assert not record["result"].get("isError")
    assert get_mcp_app_tool_result(value.metadata) == record


async def test_the_record_holds_a_copy_of_the_arguments() -> None:
    args = {"a": 1, "b": 2}
    value = await _app_return("both", args)
    args["a"] = 9
    assert value.metadata["mcp_app"]["tool_input"] == {"a": 1, "b": 2}


async def test_a_tool_with_no_view_returns_the_bare_value() -> None:
    assert await _app_return("plain", {"a": 1, "b": 2}) == {"result": 3}


async def test_a_call_with_no_tool_to_read_returns_the_bare_value() -> None:
    toolset = MCPToolset(apps_server, _request())
    assert await toolset.call_tool("plain", {"a": 1, "b": 2}, None, None) == {"result": 3}


async def test_the_flat_deprecated_view_key_links_too() -> None:
    flat = MCPServer(name="flat")
    flat.register_tool(_spec(), name="flat", description="Add.", meta={"ui/resourceUri": VIEW})
    toolset = MCPToolset(flat, _request())
    tools = await toolset.get_tools(None)  # type: ignore[arg-type]
    value = await toolset.call_tool("flat", {"a": 1, "b": 2}, None, tools["flat"])
    assert value.metadata["mcp_app"]["resource_uri"] == VIEW


async def test_the_record_rides_an_agent_run_to_the_tool_return_part() -> None:
    def model_fn(messages: Any, info: AgentInfo) -> ModelResponse:
        if any(is_tool_answer(part) for part in messages[-1].parts):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart(tool_name="both", args={"a": 1, "b": 2})])

    agent = Agent(
        FunctionModel(model_fn),
        deps_type=AgentDeps,
        toolsets=[MCPToolset(apps_server, _request())],
    )
    result = await agent.run("add", deps=AgentDeps(user=AnonymousUser()))
    (part,) = tool_answers(result.all_messages())
    assert part.content == {"result": 3}
    assert get_mcp_app_tool_result(part.metadata)["resource_uri"] == VIEW  # type: ignore[index]


def test_the_metadata_key_is_the_one_pydantic_ai_reads() -> None:
    assert MCP_APP_METADATA_KEY == "mcp_app"


@pytest.mark.parametrize("name", ["app_only", "either", "both"])
async def test_the_app_may_call_a_tool_visible_to_it(name: str) -> None:
    toolset = MCPToolset(apps_server, _request())
    result = await toolset.call_tool_for_app(name, {"a": 1, "b": 2})
    assert not isinstance(result, JsonRpcError)
    assert result["structuredContent"] == {"result": 3}


@pytest.mark.parametrize("name", ["model_only", "missing"])
async def test_the_app_is_refused_a_tool_hidden_from_it_or_unknown(name: str) -> None:
    result = await MCPToolset(apps_server, _request()).call_tool_for_app(name, {})
    assert isinstance(result, JsonRpcError)
    assert result.code == JsonRpcErrorCode.INVALID_PARAMS


async def test_a_tool_with_no_meta_is_visible_to_the_app_and_its_error_result_is_returned() -> None:
    result = await MCPToolset(apps_server, _request()).call_tool_for_app("plain", None)
    assert not isinstance(result, JsonRpcError)
    assert result["isError"] is True


async def test_non_object_arguments_are_refused() -> None:
    result = await MCPToolset(apps_server, _request()).call_tool_for_app("both", [1])  # type: ignore[arg-type]
    assert isinstance(result, JsonRpcError)
    assert result.code == JsonRpcErrorCode.INVALID_PARAMS


async def test_the_app_call_acts_as_the_requests_user() -> None:
    guarded = MCPServer(name="guarded")
    guarded.register_tool(
        ServiceSpec(
            service=add_numbers,
            reads=_TWO,
            permissions=[_Authenticated()],
            atomic=False,
        ),
        name="add",
        description="Add.",
    )
    result = await MCPToolset(guarded, _request()).call_tool_for_app("add", {"a": 1, "b": 2})
    assert isinstance(result, JsonRpcError)
    assert result.code == JsonRpcErrorCode.FORBIDDEN
    with pytest.raises(PermissionDenied):
        await MCPToolset(guarded, _request()).call_tool("add", {"a": 1, "b": 2}, None, None)


async def test_the_app_reads_its_view_in_wire_form() -> None:
    result = await MCPToolset(apps_server, _request()).read_resource_for_app(VIEW)
    assert not isinstance(result, JsonRpcError)
    (content,) = result["contents"]
    assert content["uri"] == VIEW
    assert "<p>sum</p>" in content["text"]


async def test_an_unknown_view_is_the_servers_protocol_error() -> None:
    result = await MCPToolset(apps_server, _request()).read_resource_for_app("ui://apps/none")
    assert isinstance(result, JsonRpcError)


from django_service_specs import PermissionCheck  # noqa: E402


class _Authenticated(PermissionCheck):
    def has_permission(self, principal: Any, spec: Any) -> bool:
        return bool(getattr(principal, "is_authenticated", False))


async def test_the_app_call_reuses_the_definitions_a_step_already_loaded() -> None:
    toolset = MCPToolset(apps_server, _request())
    await toolset.get_tools(None)  # type: ignore[arg-type]
    result = await toolset.call_tool_for_app("app_only", {"a": 2, "b": 2})
    assert not isinstance(result, JsonRpcError)
    assert result["structuredContent"] == {"result": 4}
