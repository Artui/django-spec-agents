from __future__ import annotations

from pydantic_ai.messages import UserPromptPart

from django_spec_agents.child_sessions.types.report_outcome import ReportOutcome
from django_spec_agents.persistence.types.thread_ref import ThreadRef


def report_frame(
    child: ThreadRef, *, agent_id: str, output: str, outcome: ReportOutcome = "ok"
) -> UserPromptPart:
    """A settled child's report to its parent, framed as upstream's ``<task_output>`` data.

    Built through ``pydantic-ai-harness``'s public ``DelegationTask.report()``, so the
    child's output never enters system voice and cannot close its own frame. The task
    id is the child's thread id. Needs the ``[harness]`` extra.
    """
    from pydantic_ai_harness.sub_agents import DelegationTask

    task = DelegationTask(
        id=child.thread_id,
        agent_name=agent_id,
        prompt="",
        conversation_id=child.thread_id,
        status="finished",
        outcome=outcome,
        output=output,
    )
    return task.report()


__all__ = ["report_frame"]
