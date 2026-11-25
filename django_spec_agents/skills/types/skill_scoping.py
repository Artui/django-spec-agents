from __future__ import annotations

from typing import Generic, Protocol, runtime_checkable

from django.http import HttpRequest

from django_spec_agents.skills.types.skill_policy import SkillT
from django_spec_agents.skills.types.skill_scope import SkillScope


@runtime_checkable
class SkillScoping(Protocol, Generic[SkillT]):
    """The optional hook a [`SkillPolicy`][django_spec_agents.skills.SkillPolicy] adds to say
    whether a skill is the caller's own or one shared with them.

    A policy that defines ``scope`` lets a transport label each skill it shows: the
    django-ag-ui skill editor carries the value as the entry's ``scope`` key. ``scope``
    returns ``"personal"`` for a skill in the caller's own scope and ``"shared"`` for one
    another scope holds (a tenant's or a company's). A policy without the method labels
    nothing, and a transport leaves the key out. It is a separate protocol because a
    Protocol has no optional members: adding ``scope`` to ``SkillPolicy`` would break
    every policy written before it.
    """

    def scope(self, request: HttpRequest, skill: SkillT) -> SkillScope:
        """``"personal"`` when `skill` is in the caller's own scope, else ``"shared"``."""
        ...


__all__ = ["SkillScoping"]
