from __future__ import annotations

from django.http import HttpRequest

from django_spec_agents.persistence.utils import (
    USER_NAMESPACE_PREFIX,
    owner_id_for,
    safe_namespace_segment,
    session_key_for,
)

_ANONYMOUS_PREFIX = "anon-"


def memory_namespace(request: HttpRequest) -> str:
    """The per-user ``namespace`` for ``pydantic_ai_harness.memory.Memory``, from a request."""
    owner_id = owner_id_for(request)
    if owner_id is not None:
        return safe_namespace_segment(USER_NAMESPACE_PREFIX, owner_id)
    return safe_namespace_segment(_ANONYMOUS_PREFIX, session_key_for(request))


__all__ = ["memory_namespace"]
