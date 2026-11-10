from __future__ import annotations

from django.apps import AppConfig


class StoreConfig(AppConfig):
    """Opt-in app providing the reference persistence models and stores."""

    name = "django_spec_agents.contrib.store"
    label = "django_spec_agents_store"
    default_auto_field = "django.db.models.BigAutoField"
    verbose_name = "django-spec-agents stores"
