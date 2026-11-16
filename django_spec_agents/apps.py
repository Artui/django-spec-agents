from __future__ import annotations

from django.apps import AppConfig
from django.core import checks


class SpecAgentsConfig(AppConfig):
    """Opt-in app for hosts that configure skills; its checks refuse a model with no policy."""

    name = "django_spec_agents"
    label = "django_spec_agents"
    verbose_name = "django-spec-agents"

    def ready(self) -> None:
        # Local: the checks import the abstract skill model, which needs a ready app registry.
        from django_spec_agents.checks import check_configured_models

        checks.register(check_configured_models)
