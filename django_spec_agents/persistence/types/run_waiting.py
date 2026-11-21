from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai.tools import DeferredToolKind


@dataclass(frozen=True)
class RunWaiting:
    """One deferred call a parked run waits on, which a person has to answer.

    ``kind`` is upstream's ``DeferredToolKind``: ``approval``, ``question`` (a tool
    that deferred with ``CallDeferred(kind="question")``) or ``external`` (a client
    tool for a detached browser run);
    [`build_run_waiting`][django_spec_agents.build_run_waiting] reads it from the
    run's ``DeferredToolRequests``. ``summary`` is the approval prompt where one is configured and
    the tool's name otherwise. ``destructive`` is
    [`ToolGuard.requires_approval`][django_spec_agents.ToolGuard.requires_approval]'s
    answer for the call's tool.
    """

    kind: DeferredToolKind
    summary: str
    tool_call_id: str
    destructive: bool


__all__ = ["RunWaiting"]
