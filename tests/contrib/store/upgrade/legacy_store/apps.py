from __future__ import annotations

from django.apps import AppConfig


class LegacyStoreConfig(AppConfig):
    """django-pydantic-agent's store app as an installed database knows it."""

    name = "tests.contrib.store.upgrade.legacy_store"
    label = "django_pydantic_agent_store"
    default_auto_field = "django.db.models.BigAutoField"
