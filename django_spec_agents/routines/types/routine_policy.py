from __future__ import annotations

from collections.abc import Iterable
from typing import Generic, Protocol, TypeVar

from django.contrib.auth.base_user import AbstractBaseUser
from django.db import models
from django.http import HttpRequest

RoutineT = TypeVar("RoutineT", bound=models.Model)


class RoutinePolicy(Protocol, Generic[RoutineT]):
    """Who sees which routines, who may write them, and whether one may fire: the consumer's."""

    def visible(self, request: HttpRequest) -> Iterable[RoutineT]:
        """Every routine the caller may see, disabled ones too."""
        ...

    def can_write(self, request: HttpRequest, routine: RoutineT) -> bool:
        """Whether the caller may save `routine`, a new unsaved one or an existing row."""
        ...

    def can_run(self, principal: AbstractBaseUser, routine: RoutineT) -> bool:
        """Whether `routine` may fire now as `principal`, its creator; asked with no request."""
        ...

    def new_routine(self, request: HttpRequest) -> RoutineT:
        """An unsaved routine carrying the scope (tenant) a create by the caller writes to."""
        ...
