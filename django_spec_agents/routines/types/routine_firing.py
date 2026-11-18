from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoutineFiring:
    """What [`run_agent_spec`][django_spec_agents.routines.run_agent_spec.run_agent_spec]
    takes: the sweep's enqueue, the routine to fire and the fresh thread it runs into."""

    routine_id: int
    thread_id: str
