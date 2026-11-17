from __future__ import annotations

from pydantic_ai.tools import DeferredToolRequests

from django_spec_agents.persistence.types.run_waiting import RunWaiting


def build_run_waiting(
    requests: DeferredToolRequests, tool_call_id: str, *, summary: str, destructive: bool
) -> RunWaiting:
    """The [`RunWaiting`][django_spec_agents.RunWaiting] a run parked on ``tool_call_id`` records.

    Its ``kind`` is ``requests.kind(tool_call_id)``, so a stored row carries upstream's
    word for the deferral; a ``tool_call_id`` the requests do not hold raises ``ValueError``.
    """
    return RunWaiting(
        kind=requests.kind(tool_call_id),
        summary=summary,
        tool_call_id=tool_call_id,
        destructive=destructive,
    )


__all__ = ["build_run_waiting"]
