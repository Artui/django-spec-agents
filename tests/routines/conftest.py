from __future__ import annotations

from datetime import time

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.http import HttpRequest
from django.test import RequestFactory
from pydantic_ai.tools import ToolDefinition

from django_spec_agents.policy.guard.tool_guard import ToolGuard
from django_spec_agents.policy.guard.types.tool_guard_config import ToolGuardConfig
from django_spec_agents.registry.tool_registry import ToolRegistry
from django_spec_agents.routines import RoutineDraft, WeeklySchedule
from django_spec_agents.types.destructive_metadata_key import DESTRUCTIVE_METADATA_KEY
from tests.routines.testapp.models import OwnRoutinesPolicy


def request_as(user: object) -> HttpRequest:
    request = RequestFactory().get("/")
    request.user = user
    return request


def tool(name: str, *, destructive: bool = False) -> ToolDefinition:
    return ToolDefinition(
        name=name, metadata={DESTRUCTIVE_METADATA_KEY: True} if destructive else None
    )


def draft(**changes: object) -> RoutineDraft:
    fields: dict[str, object] = {
        "name": "Overdue digest",
        "agent_id": "assistant",
        "prompt": "/what-is-on: tell me what is overdue",
        "schedule": WeeklySchedule(weekdays=frozenset(range(5)), at=time(8, 0)),
        "timezone": "America/New_York",
    }
    return RoutineDraft(**{**fields, **changes})


@pytest.fixture
def guard() -> ToolGuard:
    return ToolGuard(ToolRegistry(), config=ToolGuardConfig(enabled=True))


@pytest.fixture
def policy() -> OwnRoutinesPolicy:
    return OwnRoutinesPolicy()


@pytest.fixture
def member(db: None) -> AbstractBaseUser:
    return get_user_model().objects.create_user(username="member")


@pytest.fixture
def other(db: None) -> AbstractBaseUser:
    return get_user_model().objects.create_user(username="other")


@pytest.fixture
def staff(db: None) -> AbstractBaseUser:
    return get_user_model().objects.create_user(username="staff", is_staff=True)
