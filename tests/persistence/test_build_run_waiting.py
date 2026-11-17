from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai import Agent, CallDeferred, DeferredToolRequests, Tool
from pydantic_ai.models.test import TestModel

from django_spec_agents.persistence.build_run_waiting import build_run_waiting
from django_spec_agents.persistence.types.run_waiting import RunWaiting


def _approve(order_id: int) -> str:
    return "approved"


def _external(order_id: int) -> str:
    raise CallDeferred


def _question(order_id: int) -> str:
    raise CallDeferred(kind="question")


async def _requests(tool: Tool[Any]) -> DeferredToolRequests:
    agent = Agent(
        TestModel(call_tools=[tool.name]),
        tools=[tool],
        output_type=[str, DeferredToolRequests],
    )
    result = await agent.run("go")
    assert isinstance(result.output, DeferredToolRequests)
    return result.output


@pytest.mark.parametrize(
    ("tool", "kind"),
    [
        (Tool(_approve, name="drop", requires_approval=True), "approval"),
        (Tool(_external, name="drop"), "external"),
        (Tool(_question, name="drop"), "question"),
    ],
)
async def test_the_kind_is_read_from_the_runs_requests(tool: Tool[Any], kind: str) -> None:
    requests = await _requests(tool)
    [call] = [*requests.approvals, *requests.calls]

    waiting = build_run_waiting(requests, call.tool_call_id, summary="drop", destructive=True)

    assert waiting == RunWaiting(
        kind=kind,
        summary="drop",
        tool_call_id=call.tool_call_id,
        destructive=True,
    )


async def test_a_call_the_requests_do_not_hold_raises() -> None:
    requests = await _requests(Tool(_external, name="drop"))

    with pytest.raises(ValueError, match="no-such-call"):
        build_run_waiting(requests, "no-such-call", summary="drop", destructive=False)
