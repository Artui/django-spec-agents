from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from django.apps import AppConfig, apps
from django.core.checks import CheckMessage, Error
from django.core.exceptions import ImproperlyConfigured
from django.db import models

from django_spec_agents.routines.abstract_routine import AbstractRoutine
from django_spec_agents.skills.get_skill_model import get_skill_model
from django_spec_agents.skills.types.skill_model_setting import SKILL_MODEL_SETTING


@dataclass(frozen=True)
class _ConfiguredModel:
    """A setting naming a consumer's model, and the policy that model must bring."""

    setting: str
    resolve: Callable[[], type[models.Model] | None]
    policy: Callable[[Any], object]
    hint: str
    unresolved_id: str
    no_policy_id: str


_CONFIGURED_MODELS = (
    _ConfiguredModel(
        setting=SKILL_MODEL_SETTING,
        resolve=get_skill_model,
        policy=lambda model: model.skill_policy(),
        hint="Override skill_policy() on the model to return its SkillPolicy.",
        unresolved_id="django_spec_agents.E001",
        no_policy_id="django_spec_agents.E002",
    ),
)


def check_configured_models(
    app_configs: Sequence[AppConfig] | None = None, **kwargs: object
) -> list[CheckMessage]:
    """An `Error` per configured skill model or installed routine model that lacks a policy."""
    errors: list[CheckMessage] = []
    for row in _CONFIGURED_MODELS:
        try:
            model = row.resolve()
        except ImproperlyConfigured as error:
            errors.append(Error(str(error), id=row.unresolved_id))
            continue
        if model is not None and row.policy(model) is None:
            errors.append(
                Error(
                    f"{row.setting} names {model._meta.label}, which brings no policy.",
                    hint=row.hint,
                    obj=model,
                    id=row.no_policy_id,
                )
            )
    errors.extend(
        Error(
            f"{model._meta.label} subclasses AbstractRoutine and brings no policy.",
            hint="Override routine_policy() on the model to return its RoutinePolicy.",
            obj=model,
            id="django_spec_agents.E003",
        )
        for model in apps.get_models()
        if issubclass(model, AbstractRoutine) and model.routine_policy() is None
    )
    return errors
