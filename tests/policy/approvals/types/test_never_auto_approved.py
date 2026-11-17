from __future__ import annotations

from django.test import RequestFactory

from django_spec_agents.policy.approvals.types.never_auto_approved import NEVER_AUTO_APPROVED
from django_spec_agents.skills import build_skill_tools
from tests.skills.testapp.models import PersonalFirstPolicy


def test_it_names_every_skill_authoring_tool_the_toolset_gates() -> None:
    toolset = build_skill_tools(PersonalFirstPolicy(), RequestFactory().get("/"))
    gated = {name for name, tool in toolset.tools.items() if tool.metadata}

    assert gated == {"create_skill", "update_skill", "disable_skill", "enable_skill"}
    assert gated <= NEVER_AUTO_APPROVED


def test_it_names_both_routine_authoring_tools() -> None:
    assert {"create_routine", "update_routine"} <= NEVER_AUTO_APPROVED
