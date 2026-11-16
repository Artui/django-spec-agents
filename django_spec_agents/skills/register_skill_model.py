from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured

from django_spec_agents.apps import SpecAgentsConfig
from django_spec_agents.skills.abstract_skill import AbstractSkill


def register_skill_model(model: type[AbstractSkill]) -> None:
    """Make `model` the project's skill model; call it from the host's `AppConfig.ready()`.

    Registering the same model again does nothing. Raises `ImproperlyConfigured` when
    `django_spec_agents` is not in `INSTALLED_APPS`, when `model` is not a concrete
    `AbstractSkill` subclass, and when a different model is already registered.
    """
    config = SpecAgentsConfig.installed()
    if config is None:
        raise ImproperlyConfigured(
            "Add 'django_spec_agents' to INSTALLED_APPS to register a skill model: its "
            "system checks refuse a model with no policy."
        )
    config.register_model(AbstractSkill, model)
