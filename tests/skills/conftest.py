from __future__ import annotations

from collections.abc import Iterator

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractBaseUser
from django.http import HttpRequest
from django.test import RequestFactory

from tests.skills.testapp.models import PersonalFirstPolicy


@pytest.fixture(autouse=True)
def empty_registry() -> Iterator[None]:
    registered = apps.get_app_config("django_spec_agents").registered_models
    saved = dict(registered)
    registered.clear()
    yield
    registered.clear()
    registered.update(saved)


def request_as(user: AbstractBaseUser) -> HttpRequest:
    request = RequestFactory().get("/")
    request.user = user
    return request


@pytest.fixture
def policy() -> PersonalFirstPolicy:
    return PersonalFirstPolicy()


@pytest.fixture
def member(db: None) -> AbstractBaseUser:
    return get_user_model().objects.create_user(username="member")


@pytest.fixture
def admin(db: None) -> AbstractBaseUser:
    return get_user_model().objects.create_user(username="admin", is_staff=True)
