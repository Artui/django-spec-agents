from __future__ import annotations

from django.test import RequestFactory

from django_spec_agents.policy.approvals.types.never_auto_approved import NEVER_AUTO_APPROVED
from django_spec_agents.routines import ROUTINE_AUTHORING_TOOLS
from django_spec_agents.skills import build_skill_tools
from tests.skills.testapp.models import PersonalFirstPolicy


def test_it_names_every_skill_authoring_tool_the_toolset_gates() -> None:
    toolset = build_skill_tools(PersonalFirstPolicy(), RequestFactory().get("/"))
    gated = {name for name, tool in toolset.tools.items() if tool.metadata}

    assert gated == {"create_skill", "update_skill", "disable_skill", "enable_skill"}
    assert gated <= NEVER_AUTO_APPROVED


def test_it_names_every_routine_write_and_not_the_listing() -> None:
    writes = ROUTINE_AUTHORING_TOOLS - {"list_routines"}

    assert writes == {"create_routine", "update_routine", "pause_routine"}
    assert writes <= NEVER_AUTO_APPROVED
    assert "list_routines" not in NEVER_AUTO_APPROVED
