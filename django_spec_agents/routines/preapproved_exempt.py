from __future__ import annotations

from django_spec_agents.routines.abstract_routine import AbstractRoutine


def preapproved_exempt(routine: AbstractRoutine) -> frozenset[str]:
    """The names a firing's guard exempts: the granted tools that were gated when granted.

    A tool granted while ungated is left out, so if it is declared destructive later its call
    parks rather than running on a grant that never named it as destructive.
    """
    return frozenset(tool.name for tool in routine.get_preapproved_tools() if tool.destructive)
