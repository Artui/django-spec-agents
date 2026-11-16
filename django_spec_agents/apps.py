from __future__ import annotations

from types import ModuleType
from typing import cast

from django.apps import AppConfig, apps
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.db import models


class SpecAgentsConfig(AppConfig):
    """Opt-in app holding the models a host registers; its checks refuse one with no policy."""

    name = "django_spec_agents"
    label = "django_spec_agents"
    verbose_name = "django-spec-agents"

    def __init__(self, app_name: str, app_module: ModuleType | None) -> None:
        super().__init__(app_name, app_module)
        self.registered_models: dict[type[models.Model], type[models.Model]] = {}

    def ready(self) -> None:
        # Local: the checks import the abstract skill model, which needs a ready app registry.
        from django_spec_agents.checks import check_registered_models

        checks.register(check_registered_models)

    @classmethod
    def installed(cls) -> SpecAgentsConfig | None:
        """This app's config, or `None` when the host left the app out of `INSTALLED_APPS`."""
        if not apps.is_installed(cls.name):
            return None
        return cast(SpecAgentsConfig, apps.get_app_config(cls.label))

    def register_model(self, base: type[models.Model], model: object) -> None:
        """Record `model` as the concrete `base` subclass; raises `ImproperlyConfigured` if not."""
        if not (isinstance(model, type) and issubclass(model, base)) or model._meta.abstract:
            raise ImproperlyConfigured(
                f"Register a concrete subclass of {base.__name__}, not {model!r}."
            )
        current = self.registered_models.setdefault(base, model)
        if current is not model:
            raise ImproperlyConfigured(
                f"{current._meta.label} is already registered as the {base.__name__} model; "
                f"{model._meta.label} cannot replace it."
            )
