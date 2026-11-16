from __future__ import annotations

from dataclasses import dataclass

from django_spec_agents.routines.types.schedule import Schedule


@dataclass(frozen=True)
class RoutineDraft:
    """What a create or an update writes to a routine.

    `preapproved_tools` names the tools to grant; the write pins each new name's destructive
    flag through the agent's guard, and keeps the flag already pinned for a name it held.
    """

    name: str
    agent_id: str
    prompt: str
    schedule: Schedule
    timezone: str
    preapproved_tools: tuple[str, ...] = ()
    enabled: bool = True
