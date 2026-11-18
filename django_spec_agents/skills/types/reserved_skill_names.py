from __future__ import annotations

from collections.abc import Collection
from typing import Protocol, runtime_checkable

from django.http import HttpRequest


@runtime_checkable
class ReservedSkillNames(Protocol):
    """The optional hook a [`SkillPolicy`][django_spec_agents.skills.SkillPolicy] adds to keep
    the names its code-registered skills hold from being taken by a stored one.

    A policy that defines ``reserved_names`` has ``create_skill``, ``update_skill`` and every
    write through them, the skill tools and an editor endpoint alike, refuse a stored skill
    named like a reserved one. A policy without it reserves nothing.
    """

    def reserved_names(self, request: HttpRequest) -> Collection[str]:
        """The names no stored skill may take for this caller."""
        ...


__all__ = ["ReservedSkillNames"]
