from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from django.apps import AppConfig, apps
from django.core.checks import CheckMessage, Error
from django.db import models

from django_spec_agents.routines.abstract_routine import AbstractRoutine
from django_spec_agents.skills.abstract_skill import AbstractSkill


@dataclass(frozen=True)
class _GuardedBase:
    """An abstract model whose installed subclasses must each bring a policy."""

    base: type[models.Model]
    policy: Callable[[Any], object]
    hint: str
    no_policy_id: str


_GUARDED_BASES = (
    _GuardedBase(
        base=AbstractSkill,
        policy=lambda model: model.skill_policy(),
        hint="Override skill_policy() on the model to return its SkillPolicy.",
        no_policy_id="django_spec_agents.E001",
    ),
    _GuardedBase(
        base=AbstractRoutine,
        policy=lambda model: model.routine_policy(),
        hint="Override routine_policy() on the model to return its RoutinePolicy.",
        no_policy_id="django_spec_agents.E002",
    ),
)


def check_configured_models(
    app_configs: Sequence[AppConfig] | None = None, **kwargs: object
) -> list[CheckMessage]:
    """An `Error` per installed model of a guarded base that brings no policy."""
    return [
        Error(
            f"{model._meta.label} subclasses {row.base.__name__} and brings no policy.",
            hint=row.hint,
            obj=model,
            id=row.no_policy_id,
        )
        for model in apps.get_models()
        for row in _GUARDED_BASES
        if issubclass(model, row.base) and row.policy(model) is None
    ]
