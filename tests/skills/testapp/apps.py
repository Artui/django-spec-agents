from __future__ import annotations

from django.apps import AppConfig


class SkillsTestAppConfig(AppConfig):
    name = "tests.skills.testapp"
    label = "skills_testapp"
    default_auto_field = "django.db.models.AutoField"

    def ready(self) -> None:
        from django_spec_agents.skills import register_skill_model
        from tests.skills.testapp.models import TenantSkill

        register_skill_model(TenantSkill)
