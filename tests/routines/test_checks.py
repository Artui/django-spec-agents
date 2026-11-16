from __future__ import annotations

from django.apps import apps
from django.conf import settings
from django.core import checks
from django.test import override_settings

from django_spec_agents.checks import check_configured_models

WITH_UNGUARDED = [*settings.INSTALLED_APPS, "tests.routines.unguarded"]
WITH_BOTH_UNGUARDED = [*WITH_UNGUARDED, "tests.skills.unguarded"]


def test_installed_routine_models_with_a_policy_pass() -> None:
    assert apps.get_model("routines_testapp", "TeamRoutine").routine_policy() is not None
    assert check_configured_models() == []


@override_settings(INSTALLED_APPS=WITH_UNGUARDED)
def test_an_installed_routine_model_with_no_policy_fails_the_check() -> None:
    unguarded = apps.get_model("routines_unguarded", "UnguardedRoutine")
    errors = check_configured_models()

    assert [(error.id, error.obj, error.level) for error in errors] == [
        ("django_spec_agents.E002", unguarded, checks.ERROR)
    ]
    assert "routines_unguarded.UnguardedRoutine subclasses AbstractRoutine" in errors[0].msg
    assert "routine_policy()" in errors[0].hint


@override_settings(INSTALLED_APPS=WITH_BOTH_UNGUARDED)
def test_skills_and_routines_are_checked_in_one_pass() -> None:
    ids = sorted(error.id for error in check_configured_models())

    assert ids == ["django_spec_agents.E001", "django_spec_agents.E002"]


@override_settings(SPEC_AGENTS_ROUTINE_MODEL="not-a-label")
def test_no_routine_setting_is_read() -> None:
    assert check_configured_models() == []
