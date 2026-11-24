from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai_harness.detect_loops import LoopDetectedError, LoopNudgePart

from django_spec_agents.agent.agent_factory import build_agent
from django_spec_agents.agent.types.agent_config import AgentConfig
from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.policy.loops.build_detect_loops import build_detect_loops
from django_spec_agents.policy.loops.types.loop_detection_config import LoopDetectionConfig
from django_spec_agents.registry.decorator import tool
from django_spec_agents.registry.tool_registry import ToolRegistry


def read_file(path: str) -> str:
    """Read a file."""
    return "Error: file not found"


def _nudged(messages: list[ModelMessage]) -> bool:
    return any(
        "Change your approach" in str(getattr(part, "content", ""))
        for m in messages
        for part in m.parts
    )


def _stuck(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    if _nudged(messages):
        return ModelResponse(parts=[TextPart("I am stuck: the file is missing.")])
    return ModelResponse(parts=[ToolCallPart("read_file", {"path": "missing.txt"})])


def _agent(loops: LoopDetectionConfig | None) -> object:
    reg = ToolRegistry()
    tool(reg)(read_file)
    return build_agent(reg, AgentConfig(model=FunctionModel(_stuck), loop_detection=loops))


async def test_a_loop_under_action_error_ends_the_run() -> None:
    agent = _agent(LoopDetectionConfig(action="error"))

    with pytest.raises(LoopDetectedError) as caught:
        await agent.run("Summarize missing.txt", deps=AgentDeps(user=None))  # ty: ignore[unresolved-attribute]

    assert (caught.value.loop.pattern, caught.value.loop.tool_names) == (
        "repeated_result",
        ("read_file",),
    )


async def test_a_loop_under_the_default_nudge_tells_the_model_and_carries_on() -> None:
    agent = _agent(LoopDetectionConfig())

    result = await agent.run("Summarize missing.txt", deps=AgentDeps(user=None))  # ty: ignore[unresolved-attribute]

    assert result.output == "I am stuck: the file is missing."
    nudges = [p for m in result.all_messages() for p in m.parts if isinstance(p, LoopNudgePart)]
    assert [nudge.pattern for nudge in nudges] == ["repeated_result"]


async def test_a_threshold_set_to_none_turns_its_check_off() -> None:
    agent = _agent(
        LoopDetectionConfig(
            action="error",
            repeated_result_threshold=None,
            repeated_call_threshold=4,
        )
    )

    with pytest.raises(LoopDetectedError) as caught:
        await agent.run("Summarize missing.txt", deps=AgentDeps(user=None))  # ty: ignore[unresolved-attribute]

    assert (caught.value.loop.pattern, caught.value.loop.count) == ("repeated_call", 4)


def test_the_config_reaches_the_capability() -> None:
    built = build_detect_loops(
        LoopDetectionConfig(
            window=6,
            repeated_call_threshold=None,
            repeated_result_threshold=2,
            alternating_calls_threshold=None,
            repeated_text_threshold=None,
            stream_checks=False,
        )
    )

    assert (
        built.window,  # ty: ignore[unresolved-attribute]
        built.repeated_call_threshold,  # ty: ignore[unresolved-attribute]
        built.repeated_result_threshold,  # ty: ignore[unresolved-attribute]
        built.alternating_calls_threshold,  # ty: ignore[unresolved-attribute]
        built.repeated_text_threshold,  # ty: ignore[unresolved-attribute]
        built.stream_detector,  # ty: ignore[unresolved-attribute]
    ) == (6, None, 2, None, None, None)


def test_stream_checks_keep_the_harness_detector() -> None:
    built = build_detect_loops(LoopDetectionConfig())

    assert built.stream_detector is not None  # ty: ignore[unresolved-attribute]


async def test_without_loop_detection_the_agent_loops_until_its_limit() -> None:
    from pydantic_ai.exceptions import UsageLimitExceeded
    from pydantic_ai.usage import UsageLimits

    agent = _agent(None)

    with pytest.raises(UsageLimitExceeded):
        await agent.run(  # ty: ignore[unresolved-attribute]
            "Summarize missing.txt",
            deps=AgentDeps(user=None),
            usage_limits=UsageLimits(request_limit=8),
        )
