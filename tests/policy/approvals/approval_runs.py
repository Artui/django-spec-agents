from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import Agent, DeferredToolRequests, Tool, ToolReturn
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel


@dataclass
class Reviewer:
    """A reviewing model that gives one answer, or raises, and counts its calls."""

    answer: str | None
    calls: list[str] = field(default_factory=list)

    def model(self) -> FunctionModel:
        return FunctionModel(self._respond, model_name="judge")

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls.append(str(messages[-1].parts[-1].content))  # ty: ignore[unresolved-attribute]
        if self.answer is None:
            raise RuntimeError("reviewer down")
        [output] = info.output_tools
        return ModelResponse(parts=[ToolCallPart(output.name, {"response": self.answer})])


def delete_order(order_id: int) -> str:
    """Delete an order."""
    return f"deleted {order_id}"


def gated_tool(name: str, *, metadata: Any = None) -> Tool[Any]:
    def run(order_id: int) -> Any:
        if metadata is None:
            return f"{name} {order_id}"
        return ToolReturn(return_value=f"{name} {order_id}", metadata=metadata)

    return Tool(run, name=name, requires_approval=True)


async def run_calls(
    tools: Sequence[Tool[Any]], capabilities: Sequence[Any], *, call: Sequence[str]
) -> Any:
    agent = Agent(
        TestModel(call_tools=list(call)),
        tools=list(tools),
        output_type=[str, DeferredToolRequests],
        capabilities=list(capabilities),
    )
    return await agent.run("go")


def returns(result: Any) -> dict[str, ToolReturnPart]:
    return {
        part.tool_name: part
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }


def parked(result: Any) -> list[str]:
    output = result.output
    if not isinstance(output, DeferredToolRequests):
        return []
    return [call.tool_name for call in output.approvals]
