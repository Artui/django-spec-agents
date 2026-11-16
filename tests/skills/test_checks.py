from __future__ import annotations

from django.apps import apps
from django.conf import settings
from django.core import checks
from django.test import override_settings

from django_spec_agents.checks import check_configured_models

WITH_UNGUARDED = [*settings.INSTALLED_APPS, "tests.skills.unguarded"]


def test_the_app_installs_beside_the_store_under_its_own_label() -> None:
    assert apps.get_app_config("django_spec_agents").name == "django_spec_agents"
    assert apps.get_app_config("django_spec_agents_store").name == (
        "django_spec_agents.contrib.store"
    )


def test_the_check_is_registered_by_the_app() -> None:
    assert check_configured_models in checks.registry.registry.get_checks()


def test_installed_skill_models_with_a_policy_pass() -> None:
    assert apps.get_model("skills_testapp", "TenantSkill").skill_policy() is not None
    assert check_configured_models() == []


@override_settings(INSTALLED_APPS=WITH_UNGUARDED)
def test_an_installed_skill_model_with_no_policy_fails_the_check() -> None:
    unguarded = apps.get_model("skills_unguarded", "UnguardedSkill")
    errors = check_configured_models()

    assert [(error.id, error.obj, error.level) for error in errors] == [
        ("django_spec_agents.E001", unguarded, checks.ERROR)
    ]
    assert "skills_unguarded.UnguardedSkill subclasses AbstractSkill" in errors[0].msg


@override_settings(SPEC_AGENTS_SKILL_MODEL="not-a-label")
def test_no_setting_is_read() -> None:
    assert check_configured_models() == []
