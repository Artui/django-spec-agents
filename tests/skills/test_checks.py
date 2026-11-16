from __future__ import annotations

import pytest
from django.apps import apps
from django.contrib.auth.models import Group
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from django_spec_agents.checks import check_registered_models
from django_spec_agents.skills import AbstractSkill, get_skill_model, register_skill_model
from tests.skills.testapp.apps import SkillsTestAppConfig
from tests.skills.testapp.models import TenantSkill, UnguardedSkill


def test_the_app_installs_beside_the_store_under_its_own_label() -> None:
    assert apps.get_app_config("django_spec_agents").name == "django_spec_agents"
    assert apps.get_app_config("django_spec_agents_store").name == (
        "django_spec_agents.contrib.store"
    )


def test_the_check_is_registered_by_the_app() -> None:
    assert check_registered_models in checks.registry.registry.get_checks()


def test_nothing_registered_means_no_model_and_no_messages() -> None:
    assert get_skill_model() is None
    assert check_registered_models() == []


def test_a_registered_model_is_found_without_any_setting() -> None:
    register_skill_model(TenantSkill)

    assert get_skill_model() is TenantSkill
    assert check_registered_models() == []


def test_a_registered_model_with_no_policy_fails_the_check() -> None:
    register_skill_model(UnguardedSkill)
    errors = check_registered_models()

    assert [(error.id, error.obj, error.level) for error in errors] == [
        ("django_spec_agents.E001", UnguardedSkill, checks.ERROR)
    ]
    assert "skills_testapp.UnguardedSkill" in errors[0].msg


def test_the_host_app_registers_from_its_ready() -> None:
    apps.get_app_config("skills_testapp").ready()

    assert get_skill_model() is TenantSkill
    assert isinstance(apps.get_app_config("skills_testapp"), SkillsTestAppConfig)


def test_registering_the_same_model_again_is_a_no_op() -> None:
    register_skill_model(TenantSkill)
    register_skill_model(TenantSkill)

    assert get_skill_model() is TenantSkill


def test_a_second_different_model_is_refused() -> None:
    register_skill_model(TenantSkill)

    with pytest.raises(ImproperlyConfigured, match="UnguardedSkill cannot replace it"):
        register_skill_model(UnguardedSkill)
    assert get_skill_model() is TenantSkill


@pytest.mark.parametrize(
    "model", [AbstractSkill, Group, TenantSkill(), "skills_testapp.TenantSkill"]
)
def test_anything_but_a_concrete_skill_model_is_refused(model: object) -> None:
    with pytest.raises(ImproperlyConfigured, match="concrete subclass of AbstractSkill"):
        register_skill_model(model)  # ty: ignore[invalid-argument-type]
    assert get_skill_model() is None


def test_without_the_app_installed_nothing_registers_or_resolves() -> None:
    register_skill_model(UnguardedSkill)
    left_out = {"django_spec_agents", "skills_testapp"}
    installed = [app.name for app in apps.get_app_configs() if app.label not in left_out]

    with override_settings(INSTALLED_APPS=installed):
        with pytest.raises(ImproperlyConfigured, match="INSTALLED_APPS"):
            register_skill_model(TenantSkill)
        assert get_skill_model() is None
        assert check_registered_models() == []
