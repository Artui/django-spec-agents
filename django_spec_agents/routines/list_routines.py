from __future__ import annotations

from django.http import HttpRequest

from django_spec_agents.routines.types.routine_policy import RoutinePolicy
from django_spec_agents.routines.types.routine_t import RoutineT


def list_routines(policy: RoutinePolicy[RoutineT], request: HttpRequest) -> list[RoutineT]:
    """The routines the caller may see whose principal is the caller, disabled ones too.

    A routine the policy shows the caller but that runs as someone else, a teammate's, is left
    out; an anonymous caller has none.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return []
    return [routine for routine in policy.visible(request) if routine.principal_id == user.pk]
