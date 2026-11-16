from __future__ import annotations

import pytest
from django.contrib.auth.base_user import AbstractBaseUser

from django_spec_agents.policy.guard.tool_guard import ToolGuard
from django_spec_agents.policy.guard.types.tool_guard_config import ToolGuardConfig
from django_spec_agents.registry.tool_registry import ToolRegistry
from django_spec_agents.routines import (
    InvalidRoutineError,
    PreapprovedTool,
    create_routine,
    pin_preapproved_tools,
    preapproved_exempt,
    update_routine,
)
from tests.routines.conftest import draft, request_as, tool
from tests.routines.testapp.models import OwnRoutinesPolicy


def test_the_grant_pins_each_tools_flag_from_the_guard(guard: ToolGuard) -> None:
    offered = [tool("search"), tool("delete_invoice", destructive=True)]

    assert pin_preapproved_tools(guard, offered, ["search", "delete_invoice"]) == (
        PreapprovedTool(name="search", destructive=False),
        PreapprovedTool(name="delete_invoice", destructive=True),
    )


def test_the_grant_reads_the_guards_whole_precedence() -> None:
    config = ToolGuardConfig(
        enabled=True, require_approval=frozenset({"export"}), exempt=frozenset({"purge"})
    )
    guard = ToolGuard(ToolRegistry(), config=config)
    offered = [tool("export"), tool("purge", destructive=True)]

    assert pin_preapproved_tools(guard, offered, ["export", "purge"]) == (
        PreapprovedTool(name="export", destructive=True),
        PreapprovedTool(name="purge", destructive=False),
    )


def test_a_name_the_agent_does_not_offer_is_refused(guard: ToolGuard) -> None:
    with pytest.raises(InvalidRoutineError, match="no tool named delte, nope"):
        pin_preapproved_tools(guard, [tool("delete")], ["nope", "delete", "delte"])


def test_the_grant_reads_names_from_a_one_shot_iterable(guard: ToolGuard) -> None:
    pinned = pin_preapproved_tools(guard, iter([tool("a")]), (name for name in ["a"]))

    assert pinned == (PreapprovedTool(name="a", destructive=False),)


def test_only_tools_gated_when_granted_are_exempt(
    policy: OwnRoutinesPolicy, member: AbstractBaseUser, guard: ToolGuard
) -> None:
    offered = [tool("search"), tool("delete_invoice", destructive=True)]

    routine = create_routine(
        policy,
        request_as(member),
        draft(preapproved_tools=("search", "delete_invoice")),
        guard=guard,
        tool_defs=offered,
    )

    assert routine.preapproved_tools == [
        {"name": "search", "destructive": False},
        {"name": "delete_invoice", "destructive": True},
    ]
    assert preapproved_exempt(routine) == frozenset({"delete_invoice"})


def test_an_edit_keeps_the_flag_pinned_when_the_tool_was_granted(
    policy: OwnRoutinesPolicy, member: AbstractBaseUser, guard: ToolGuard
) -> None:
    request = request_as(member)
    routine = create_routine(
        policy,
        request,
        draft(preapproved_tools=("archive",)),
        guard=guard,
        tool_defs=[tool("archive")],
    )
    redeclared = [tool("archive", destructive=True), tool("send", destructive=True)]

    update_routine(
        policy,
        request,
        routine,
        draft(prompt="A new prompt", preapproved_tools=("archive", "send")),
        guard=guard,
        tool_defs=redeclared,
    )
    routine.refresh_from_db()

    assert routine.get_preapproved_tools() == (
        PreapprovedTool(name="archive", destructive=False),
        PreapprovedTool(name="send", destructive=True),
    )
    assert preapproved_exempt(routine) == frozenset({"send"})


def test_a_name_dropped_from_the_grant_is_revoked_and_pins_afresh_when_named_again(
    policy: OwnRoutinesPolicy, member: AbstractBaseUser, guard: ToolGuard
) -> None:
    request = request_as(member)
    routine = create_routine(
        policy,
        request,
        draft(preapproved_tools=("archive",)),
        guard=guard,
        tool_defs=[tool("archive")],
    )
    redeclared = [tool("archive", destructive=True)]

    update_routine(policy, request, routine, draft(), guard=guard, tool_defs=redeclared)
    assert routine.preapproved_tools == []
    update_routine(
        policy,
        request,
        routine,
        draft(preapproved_tools=("archive",)),
        guard=guard,
        tool_defs=redeclared,
    )

    assert preapproved_exempt(routine) == frozenset({"archive"})
