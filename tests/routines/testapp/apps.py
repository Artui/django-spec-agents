from __future__ import annotations

from django.apps import AppConfig


class RoutinesTestAppConfig(AppConfig):
    name = "tests.routines.testapp"
    label = "routines_testapp"
    default_auto_field = "django.db.models.AutoField"
