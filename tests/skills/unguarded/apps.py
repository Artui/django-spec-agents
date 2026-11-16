from __future__ import annotations

from django.apps import AppConfig


class UnguardedSkillsConfig(AppConfig):
    name = "tests.skills.unguarded"
    label = "skills_unguarded"
    default_auto_field = "django.db.models.AutoField"
