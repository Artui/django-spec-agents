from __future__ import annotations

from pydantic_ai.framing import register_tag, wrap
from pydantic_ai.messages import TextContent, UserPromptPart

from django_spec_agents.child_sessions.types.message_source import MessageSource
from django_spec_agents.child_sessions.types.message_source_key import MESSAGE_SOURCE_KEY
from django_spec_agents.child_sessions.types.report_outcome import ReportOutcome
from django_spec_agents.persistence.types.thread_ref import ThreadRef

_TASK_OUTPUT = register_tag("task_output")
register_tag("task_question")


def report_frame(
    child: ThreadRef, *, agent_id: str, output: str, outcome: ReportOutcome = "ok"
) -> UserPromptPart:
    """A settled child's report to its parent, framed as ``<task_output>`` data.

    The frame is upstream's ``DelegationTask.report()`` tag and attributes, rendered with
    ``pydantic_ai.framing.wrap`` so the text keeps its escaping once it crosses a queue: the
    child's output never enters system voice, cannot close its own frame and cannot forge a
    ``<task_question>``. The task id is the child's thread id, and ``outcome`` is on the tag
    only when it is not ``ok``. The text's ``TextContent.metadata`` names its source,
    ``MESSAGE_SOURCE_KEY`` mapped to ``MessageSource.REPORT``, which the model never sees.
    """
    text = wrap(
        _TASK_OUTPUT,
        f"\n{output}\n",
        task_id=child.thread_id,
        agent_name=agent_id,
        outcome=None if outcome == "ok" else outcome,
    )
    mark = {MESSAGE_SOURCE_KEY: MessageSource.REPORT.value}
    return UserPromptPart([TextContent(text, metadata=mark)])


__all__ = ["report_frame"]
