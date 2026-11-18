from __future__ import annotations

from django.http import HttpRequest

from django_spec_agents.routines.routine_write_refused_error import RoutineWriteRefusedError
from django_spec_agents.routines.types.routine_policy import RoutinePolicy
from django_spec_agents.routines.types.routine_t import RoutineT
from django_spec_agents.routines.utils import require_write


def pause_routine(
    policy: RoutinePolicy[RoutineT], request: HttpRequest, routine: RoutineT
) -> RoutineT:
    """Disable `routine`, if `can_write` admits it, keeping its prompt, schedule and grant.

    `next_fire_at` clears, so the sweep skips it until an update enables it again. `can_write`
    is asked of the row as stored and of the result; a refusal leaves `routine` as it was and
    raises `RoutineWriteRefusedError`.
    """
    require_write(policy, request, routine)
    stored = (routine.enabled, routine.next_fire_at)
    routine.enabled = False
    routine.next_fire_at = None
    try:
        require_write(policy, request, routine)
    except RoutineWriteRefusedError:
        routine.enabled, routine.next_fire_at = stored
        raise
    routine.save()
    return routine
