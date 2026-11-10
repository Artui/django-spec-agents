from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import ToolDefinition

from django_spec_agents.agent.agent_factory import build_agent
from django_spec_agents.agent.types.agent_config import AgentConfig
from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.policy.audit.types.audit_event import AuditEvent
from django_spec_agents.policy.guard.types.tool_guard_config import ToolGuardConfig
from django_spec_agents.registry.decorator import tool
from django_spec_agents.registry.tool_registry import ToolRegistry


def _recorder() -> tuple[FunctionModel, list[ToolDefinition]]:
    """A model that answers at once and records the tools it was offered."""
    seen: list[ToolDefinition] = []

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(info.function_tools)
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(model_fn), seen


async def _offered(registry: ToolRegistry) -> dict[str, ToolDefinition]:
    """The ``ToolDefinition`` of each tool a model is offered, keyed by name."""
    model, seen = _recorder()
    await build_agent(registry, AgentConfig(model=model)).run("go", deps=AgentDeps(user=None))
    return {t.name: t for t in seen}


async def _offered_as_bare_functions(*fns: Callable[..., Any]) -> dict[str, ToolDefinition]:
    """What pydantic-ai offers for the same functions handed over undecorated."""
    model, seen = _recorder()
    await Agent(model, tools=list(fns)).run("go")
    return {t.name: t for t in seen}


async def test_a_destructive_tool_registered_under_a_name_is_deferred_for_approval() -> None:
    # The guard keys destructive tools by registered name, so the model must see that name.
    ran: list[str] = []
    registry = ToolRegistry()

    @tool(registry, name="purge_everything", destructive=True)
    def wipe(target: str) -> str:
        """Delete it all."""
        ran.append(target)
        return "gone"

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        # Calls the one tool it is offered, by the name it is offered it under.
        if any(isinstance(part, ToolReturnPart) for m in messages for part in m.parts):
            return ModelResponse(parts=[TextPart("done")])
        offered = [t.name for t in info.function_tools]
        return ModelResponse(parts=[ToolCallPart(offered[0], {"target": "all"})])

    agent = build_agent(
        registry,
        AgentConfig(model=FunctionModel(model_fn), tool_guard=ToolGuardConfig(enabled=True)),
    )
    result = await agent.run("go", deps=AgentDeps(user=None))

    assert isinstance(result.output, DeferredToolRequests)
    assert [call.tool_name for call in result.output.approvals] == ["purge_everything"]
    assert ran == []


async def test_the_model_is_offered_the_registered_name() -> None:
    registry = ToolRegistry()

    @tool(registry, name="renamed")
    def original(x: int) -> int:
        """Doc."""
        return x

    offered = await _offered(registry)

    assert list(offered) == ["renamed"]


async def test_the_model_is_offered_an_overriding_description() -> None:
    registry = ToolRegistry()

    @tool(registry, description="What the model should read.")
    def documented(x: int) -> int:
        """The docstring.

        A second paragraph pydantic-ai would otherwise include.
        """
        return x

    offered = await _offered(registry)

    assert offered["documented"].description == "What the model should read."


async def test_an_override_equal_to_the_first_paragraph_is_not_told_apart_from_none() -> None:
    # Equal to the docstring's first paragraph, a description derives no override.
    registry = ToolRegistry()

    @tool(registry, description="First.")
    def twin(x: int) -> int:
        """First.

        Second.
        """
        return x

    assert (await _offered(registry))["twin"].description == "First.\n\nSecond."


async def test_a_tool_with_no_override_is_offered_what_pydantic_ai_derives() -> None:
    # Registering must not change what the model sees.
    registry = ToolRegistry()

    @tool(registry)
    def documented(x: int, y: str = "q") -> int:
        """Do a thing.

        Continues here with more detail.
        Second line.

        Args:
            x: the x.
            y: the y.
        """
        return x

    @tool(registry)
    def bare(x: int) -> int:
        return x

    offered = await _offered(registry)

    assert offered == await _offered_as_bare_functions(documented, bare)

    assert offered["documented"].name == "documented"
    assert offered["documented"].description == (
        "Do a thing.\n\nContinues here with more detail.\nSecond line."
    )
    assert offered["documented"].parameters_json_schema == {
        "additionalProperties": False,
        "properties": {
            "x": {"description": "the x.", "type": "integer"},
            "y": {"default": "q", "description": "the y.", "type": "string"},
        },
        "required": ["x"],
        "type": "object",
    }
    assert offered["bare"].description is None


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)


async def test_the_audit_record_carries_the_registered_name() -> None:
    registry = ToolRegistry()

    @tool(registry, name="renamed")
    def original(x: int) -> int:
        """Doc."""
        return x

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if any(isinstance(part, ToolReturnPart) for m in messages for part in m.parts):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart(info.function_tools[0].name, {"x": 1})])

    sink = _Sink()
    agent = build_agent(registry, AgentConfig(model=FunctionModel(model_fn), audit_logger=sink))
    await agent.run("go", deps=AgentDeps(user=None))

    assert [event.tool_name for event in sink.events] == ["renamed"]


async def test_the_tool_catalog_and_the_model_agree_on_the_name() -> None:
    from django_spec_agents.agent.build_tool_catalog import build_tool_catalog

    registry = ToolRegistry()

    @tool(registry, name="renamed")
    def original(x: int) -> int:
        """Doc."""
        return x

    catalog: list[dict[str, Any]] = build_tool_catalog(registry)

    assert [entry["name"] for entry in catalog] == list(await _offered(registry))
