from __future__ import annotations

from django.apps import AppConfig


class LegacyStoreConfig(AppConfig):
    """django-pydantic-agent's store app as an installed database knows it.

    Only its label and its five migrations matter: Django keys an installed
    database on the label, and builds the schema from the migrations. It has no
    models, so nothing in it can drift from what django-pydantic-agent shipped.
    """

    name = "tests.contrib.store.upgrade.legacy_store"
    label = "django_pydantic_agent_store"
    default_auto_field = "django.db.models.BigAutoField"
