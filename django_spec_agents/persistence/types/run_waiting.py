from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunWaiting:
    """What a parked run waits on: one deferred call a person has to answer.

    ``kind`` names the deferral (an approval, or a client tool for a detached
    browser run). ``summary`` is the approval prompt where one is configured and
    the tool's name otherwise. ``destructive`` is
    [`ToolGuard.requires_approval`][django_spec_agents.ToolGuard.requires_approval]'s
    answer for the call's tool.
    """

    kind: str
    summary: str
    tool_call_id: str
    destructive: bool


__all__ = ["RunWaiting"]
