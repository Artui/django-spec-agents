from __future__ import annotations

from collections.abc import Iterable
from typing import Generic, Protocol, TypeVar

from django.db import models
from django.http import HttpRequest

SkillT = TypeVar("SkillT", bound=models.Model)


class SkillPolicy(Protocol, Generic[SkillT]):
    """Who sees which skills, and who may write where: the consumer's, never a default."""

    def visible(self, request: HttpRequest) -> Iterable[SkillT]:
        """Every skill the caller may see, disabled ones too, the winning one first per name."""
        ...

    def can_write(self, request: HttpRequest, skill: SkillT) -> bool:
        """Whether the caller may save `skill`, a new unsaved one or an existing row."""
        ...

    def new_skill(self, request: HttpRequest) -> SkillT:
        """An unsaved skill carrying the scope (tenant, owner) a create by the caller writes to."""
        ...
