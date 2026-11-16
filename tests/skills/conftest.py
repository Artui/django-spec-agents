from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractBaseUser
from django.http import HttpRequest
from django.test import RequestFactory

from tests.skills.testapp.models import PersonalFirstPolicy


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
