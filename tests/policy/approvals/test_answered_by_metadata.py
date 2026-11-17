from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from pydantic_ai import Agent, DeferredToolRequests, Tool, ToolReturn
from pydantic_ai._mcp_apps import get_mcp_app_tool_result
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.integrations.mcp import MCPToolset
from django_spec_agents.policy.approvals.answered_by_metadata import AnsweredByMetadata
from django_spec_agents.policy.approvals.granted_approvals import GrantedApprovals
from tests.integrations.test_mcp_apps import VIEW, _request, apps_server
from tests.policy.approvals.approval_runs import gated_tool, returns, run_calls


async def _granted_return(metadata: Any, *capabilities: Any) -> Any:
    result = await run_calls(
        [gated_tool("show_order", metadata=metadata)],
        [GrantedApprovals(["show_order"], "routine:7"), *capabilities],
        call=["show_order"],
    )
    return returns(result)["show_order"]


async def test_a_tools_own_dict_metadata_survives_the_merge() -> None:
    own = {"mcp_app": {"uri": "ui://orders/show"}}
    part = await _granted_return(own, AnsweredByMetadata())

    assert part.content == "show_order 0"
    assert part.metadata == {"mcp_app": {"uri": "ui://orders/show"}, "answered_by": "routine:7"}
    assert own == {"mcp_app": {"uri": "ui://orders/show"}}


@pytest.mark.parametrize("metadata", [["not", "a", "dict"], "text"])
async def test_metadata_that_is_not_a_dict_is_left_alone(metadata: Any) -> None:
    part = await _granted_return(metadata, AnsweredByMetadata())

    assert part.metadata == metadata


async def test_without_the_capability_the_history_has_no_mark() -> None:
    part = await _granted_return(None)

    assert part.metadata is None


async def test_a_tool_return_with_no_metadata_gets_the_mark() -> None:
    def show_order(order_id: int) -> ToolReturn:
        return ToolReturn(return_value=f"order {order_id}")

    result = await run_calls(
        [Tool(show_order, requires_approval=True)],
        [GrantedApprovals(["show_order"], "routine:7"), AnsweredByMetadata()],
        call=["show_order"],
    )

    assert returns(result)["show_order"].metadata == {"answered_by": "routine:7"}


async def test_an_unmarked_call_keeps_its_result() -> None:
    agent = Agent(
        TestModel(call_tools=["plain"]),
        tools=[Tool(lambda: "ok", name="plain")],
        capabilities=[AnsweredByMetadata()],
    )
    result = await agent.run("go")

    part = returns(result)["plain"]
    assert part.content == "ok"
    assert part.metadata is None


async def test_an_mcp_apps_record_survives_an_approval_answered_in_the_run() -> None:
    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if any(isinstance(part, ToolReturnPart) for m in messages for part in m.parts):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart(tool_name="both", args={"a": 1, "b": 2})])

    gated = MCPToolset(apps_server, _request()).approval_required(lambda *_: True)
    agent = Agent(
        FunctionModel(model_fn),
        deps_type=AgentDeps,
        toolsets=[gated],
        output_type=[str, DeferredToolRequests],
        capabilities=[GrantedApprovals(["both"], "routine:7"), AnsweredByMetadata()],
    )
    result = await agent.run("add", deps=AgentDeps(user=AnonymousUser()))

    part = returns(result)["both"]
    assert part.content == {"result": 3}
    assert get_mcp_app_tool_result(part.metadata)["resource_uri"] == VIEW  # ty: ignore[not-subscriptable]
    assert part.metadata["answered_by"] == "routine:7"  # ty: ignore[not-subscriptable]
