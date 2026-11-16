from __future__ import annotations

from django.apps import AppConfig


class SkillsTestAppConfig(AppConfig):
    name = "tests.skills.testapp"
    label = "skills_testapp"
    default_auto_field = "django.db.models.AutoField"
