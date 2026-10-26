"""What both integration routes' tests drive alike: a real run context, and a
run in which a model leaves out an argument and then sends it.

The calls are shared and the declarations are per route: the drf-mcp bridge's
tests serve the specs in ``drf_specs_lookup``, which that bridge registers, and
the spec capability's serve the kernel's own in ``kernel_specs``. The cross-route
tests in ``test_drf_mcp`` also run ``build_spec_capability`` over the drfs specs
on purpose, to exercise them through the ``[drf-mcp]`` bridge."""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from django_spec_agents.agent.types.agent_deps import AgentDeps
from tests.tool_retries import is_tool_answer, tool_answers, tool_retries


def run_context(deps: AgentDeps) -> RunContext[AgentDeps]:
    """A real ``RunContext``, not a stand-in shaped like one.

    A double carrying only ``deps`` agrees with the caller until the caller reads
    one more field, and the spec tools do: they stamp run correlation on their
    log lines from ``run_id`` / ``conversation_id`` / ``run_step`` /
    ``tool_call_id``. This builds the genuine article and lets pydantic-ai own
    its own defaults.
    """
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


# Each tool reads its row by a ``pk`` it requires: the selector tool's own
# selector, and the service tool's target lookup. Over drf-mcp that is a
# selector taking ``pk`` with no default; on the kernel's specs it is a read
# declared ``required=True``. A call leaving ``pk`` out is corrected by sending it.
LOOKUP_CALLS = [
    pytest.param("get_row", {}, {"pk": 1}, {"id": 1, "name": "first"}, id="selector"),
    pytest.param(
        "rename_row",
        {"name": "second"},
        {"pk": 1, "name": "second"},
        {"id": 1, "name": "second"},
        id="service-target-lookup",
    ),
]


async def run_omission_then_correction(
    name: str,
    omitted: dict[str, Any],
    corrected: dict[str, Any],
    *,
    toolsets: list[Any] | None = None,
    capabilities: list[Any] | None = None,
) -> tuple[list[str], list[Any]]:
    """Run ``name`` called with ``omitted``, then with ``corrected``.

    Returns what the model was told to retry with, and the tool's returns.
    """
    calls = iter([omitted, corrected])

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if any(is_tool_answer(part) for part in messages[-1].parts):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart(tool_name=name, args=next(calls))])

    agent = Agent(
        FunctionModel(model_fn),
        deps_type=AgentDeps,
        toolsets=toolsets,
        capabilities=capabilities,
    )
    result = await agent.run("read the row", deps=AgentDeps(user=AnonymousUser()))

    retries = [str(part.content) for part in tool_retries(result.all_messages())]
    returns = [part.content for part in tool_answers(result.all_messages())]
    return retries, returns
