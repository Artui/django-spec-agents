from __future__ import annotations

from django.apps import AppConfig


class TestAppConfig(AppConfig):
    name = "tests.spec_tools.testapp"
    default_auto_field = "django.db.models.BigAutoField"
