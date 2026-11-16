from __future__ import annotations

import pytest
from django.apps import apps
from django.conf import settings
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from django_spec_agents.checks import check_configured_models
from django_spec_agents.skills import get_skill_model
from tests.routines.testapp.models import TeamRoutine
from tests.skills.testapp.models import TenantSkill, UnguardedSkill

UNGUARDED = "tests.routines.unguarded"


def test_the_app_installs_beside_the_store_under_its_own_label() -> None:
    assert apps.get_app_config("django_spec_agents").name == "django_spec_agents"
    assert apps.get_app_config("django_spec_agents_store").name == (
        "django_spec_agents.contrib.store"
    )


def test_the_check_is_registered_by_the_app() -> None:
    assert check_configured_models in checks.registry.registry.get_checks()


def test_no_skill_setting_means_no_messages() -> None:
    assert get_skill_model() is None
    assert check_configured_models() == []


@override_settings(SPEC_AGENTS_SKILL_MODEL="skills_testapp.TenantSkill")
def test_a_configured_model_with_a_policy_passes() -> None:
    assert get_skill_model() is TenantSkill
    assert check_configured_models() == []


@override_settings(SPEC_AGENTS_SKILL_MODEL="skills_testapp.UnguardedSkill")
def test_a_configured_model_with_no_policy_fails_the_check() -> None:
    errors = check_configured_models()

    assert [(error.id, error.obj, error.level) for error in errors] == [
        ("django_spec_agents.E002", UnguardedSkill, checks.ERROR)
    ]
    assert "skills_testapp.UnguardedSkill" in errors[0].msg


@pytest.mark.parametrize(
    ("label", "message"),
    [
        ("skills_testapp.Missing", "of an installed model"),
        ("not-a-label", "of an installed model"),
        (5, "of an installed model"),
        ("auth.Group", "does not subclass AbstractSkill"),
    ],
)
def test_a_setting_that_names_no_skill_model_fails_the_check(label: object, message: str) -> None:
    with override_settings(SPEC_AGENTS_SKILL_MODEL=label):
        with pytest.raises(ImproperlyConfigured, match=message):
            get_skill_model()
        errors = check_configured_models()

    assert [error.id for error in errors] == ["django_spec_agents.E001"]
    assert message in errors[0].msg


def test_every_installed_routine_model_with_a_policy_passes() -> None:
    assert TeamRoutine.routine_policy() is not None
    assert check_configured_models() == []


def test_an_installed_routine_model_with_no_policy_fails_the_check() -> None:
    with override_settings(INSTALLED_APPS=[*settings.INSTALLED_APPS, UNGUARDED]):
        unguarded = apps.get_model("routines_unguarded", "UnguardedRoutine")
        errors = check_configured_models()

    assert [(error.id, error.obj, error.level) for error in errors] == [
        ("django_spec_agents.E003", unguarded, checks.ERROR)
    ]
    assert "routines_unguarded.UnguardedRoutine" in errors[0].msg
    assert "routine_policy()" in errors[0].hint


@override_settings(SPEC_AGENTS_SKILL_MODEL="skills_testapp.UnguardedSkill")
def test_skills_and_routines_are_checked_in_one_pass() -> None:
    with override_settings(INSTALLED_APPS=[*settings.INSTALLED_APPS, UNGUARDED]):
        ids = [error.id for error in check_configured_models()]

    assert ids == ["django_spec_agents.E002", "django_spec_agents.E003"]
