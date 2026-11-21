from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import DeferredToolRequests
from pydantic_ai.exceptions import RunTimedOut
from pydantic_ai.messages import ModelMessage

from django_spec_agents.agent.agent_factory import build_agent
from django_spec_agents.agent.types.agent_config import AgentConfig
from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.headless.types.headless_start import HeadlessStart
from django_spec_agents.headless.types.run_stop import RunStop
from django_spec_agents.persistence.types.child_lineage_record import ChildLineageRecord
from django_spec_agents.persistence.types.run_outcome import RunOutcome
from django_spec_agents.persistence.types.run_waiting import RunWaiting
from django_spec_agents.persistence.types.thread_ref import ThreadRef
from django_spec_agents.policy.guard.types.tool_guard_config import ToolGuardConfig
from django_spec_agents.registry.tool_registry import ToolRegistry


@dataclass
class Call:
    agent_id: str
    prompt: str
    principal: Any
    timeout: float
    thread_id: str | None
    capabilities: Sequence[Any]


@dataclass
class AgentRunner:
    """A HeadlessRunner over a real `build_agent` run, keeping each thread's messages."""

    model: Any
    registry: ToolRegistry = field(default_factory=ToolRegistry)
    toolsets: Sequence[Any] = ()
    calls: list[Call] = field(default_factory=list)
    threads: dict[str, list[ModelMessage]] = field(default_factory=dict)

    async def start(self, *args: Any, **kwargs: Any) -> HeadlessStart:
        raise NotImplementedError

    async def run_to_first_stop(
        self,
        agent_id: str,
        prompt: str,
        principal: Any,
        *,
        timeout: float,
        thread_id: str | None = None,
        parent: ThreadRef | None = None,
        lineage: ChildLineageRecord | None = None,
        capabilities: Sequence[Any] = (),
        run_id: str | None = None,
    ) -> RunStop:
        self.calls.append(Call(agent_id, prompt, principal, timeout, thread_id, capabilities))
        config = AgentConfig(
            model=self.model,
            tool_guard=ToolGuardConfig(enabled=True),
            toolsets=list(self.toolsets),
        )
        agent = build_agent(self.registry, config)
        thread = thread_id or "t-new"
        try:
            result = await agent.run(
                prompt,
                deps=AgentDeps(user=principal),
                timeout=timeout,
                capabilities=list(capabilities),
            )
        except RunTimedOut:
            return RunStop(thread, "r1", RunOutcome.TIMED_OUT)
        except Exception:
            return RunStop(thread, "r1", RunOutcome.ERROR)
        self.threads[thread] = result.all_messages()
        if isinstance(result.output, DeferredToolRequests):
            [call] = result.output.approvals
            waiting = RunWaiting("approval", call.tool_name, call.tool_call_id, True)
            return RunStop(thread, "r1", RunOutcome.WAITING, (waiting,))
        return RunStop(thread, "r1", RunOutcome.COMPLETED)

    async def cancel(self, thread_id: str, principal: Any) -> bool:
        raise NotImplementedError
