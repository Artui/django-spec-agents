from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from django.apps import AppConfig
from django.core.checks import CheckMessage, Error
from django.db import models

from django_spec_agents.apps import SpecAgentsConfig
from django_spec_agents.skills.abstract_skill import AbstractSkill


@dataclass(frozen=True)
class _RegisteredModel:
    """A model a host registers from code against `base`, and the policy it must bring."""

    base: type[models.Model]
    registered_by: str
    policy: Callable[[Any], object]
    hint: str
    no_policy_id: str


_REGISTERED_MODELS = (
    _RegisteredModel(
        base=AbstractSkill,
        registered_by="register_skill_model",
        policy=lambda model: model.skill_policy(),
        hint="Override skill_policy() on the model to return its SkillPolicy.",
        no_policy_id="django_spec_agents.E001",
    ),
)


def check_registered_models(
    app_configs: Sequence[AppConfig] | None = None, **kwargs: object
) -> list[CheckMessage]:
    """An `Error` for each registered model that brings no policy."""
    config = SpecAgentsConfig.installed()
    registered = {} if config is None else config.registered_models
    errors: list[CheckMessage] = []
    for row in _REGISTERED_MODELS:
        model = registered.get(row.base)
        if model is not None and row.policy(model) is None:
            errors.append(
                Error(
                    f"{model._meta.label}, registered by {row.registered_by}(), brings no policy.",
                    hint=row.hint,
                    obj=model,
                    id=row.no_policy_id,
                )
            )
    return errors
