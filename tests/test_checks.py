from __future__ import annotations

import pytest
from django.apps import apps
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from django_spec_agents.checks import check_configured_models
from django_spec_agents.routines import get_routine_model
from django_spec_agents.skills import get_skill_model
from tests.routines.testapp.models import TeamRoutine, UnguardedRoutine
from tests.skills.testapp.models import TenantSkill, UnguardedSkill


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


def test_no_routine_setting_means_no_routine_messages() -> None:
    assert get_routine_model() is None


@override_settings(SPEC_AGENTS_ROUTINE_MODEL="routines_testapp.TeamRoutine")
def test_a_configured_routine_model_with_a_policy_passes() -> None:
    assert get_routine_model() is TeamRoutine
    assert check_configured_models() == []


@override_settings(SPEC_AGENTS_ROUTINE_MODEL="routines_testapp.UnguardedRoutine")
def test_a_configured_routine_model_with_no_policy_fails_the_check() -> None:
    errors = check_configured_models()

    assert [(error.id, error.obj, error.level) for error in errors] == [
        ("django_spec_agents.E004", UnguardedRoutine, checks.ERROR)
    ]
    assert "routines_testapp.UnguardedRoutine" in errors[0].msg
    assert "routine_policy()" in errors[0].hint


@override_settings(
    SPEC_AGENTS_SKILL_MODEL="skills_testapp.UnguardedSkill",
    SPEC_AGENTS_ROUTINE_MODEL="routines_testapp.UnguardedRoutine",
)
def test_both_settings_are_checked_in_one_pass() -> None:
    assert [error.id for error in check_configured_models()] == [
        "django_spec_agents.E002",
        "django_spec_agents.E004",
    ]


@pytest.mark.parametrize(
    ("label", "message"),
    [
        ("routines_testapp.Missing", "of an installed model"),
        ("not-a-label", "of an installed model"),
        (5, "of an installed model"),
        ("skills_testapp.TenantSkill", "does not subclass AbstractRoutine"),
    ],
)
def test_a_setting_that_names_no_routine_model_fails_the_check(label: object, message: str) -> None:
    with override_settings(SPEC_AGENTS_ROUTINE_MODEL=label):
        with pytest.raises(ImproperlyConfigured, match=message):
            get_routine_model()
        errors = check_configured_models()

    assert [error.id for error in errors] == ["django_spec_agents.E003"]
    assert message in errors[0].msg
