from __future__ import annotations

from django.apps import AppConfig


class UnguardedRoutinesConfig(AppConfig):
    name = "tests.routines.unguarded"
    label = "routines_unguarded"
    default_auto_field = "django.db.models.AutoField"
