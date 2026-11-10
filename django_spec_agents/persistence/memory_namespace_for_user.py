from __future__ import annotations

from typing import Any

from django_spec_agents.persistence.utils import USER_NAMESPACE_PREFIX, safe_namespace_segment

# One shared segment for every unauthenticated caller. Unlike the request-bound
# resolver there is no session to key on here, so this cannot be a per-visitor
# bucket and does not pretend to be.
_ANONYMOUS = "anon"


def memory_namespace_for_user(user: Any) -> str:
    """The ``namespace`` for ``pydantic_ai_harness.memory.Memory``, from the acting user."""
    if user is None or not getattr(user, "is_authenticated", False):
        return _ANONYMOUS
    return safe_namespace_segment(USER_NAMESPACE_PREFIX, str(user.pk))


__all__ = ["memory_namespace_for_user"]
