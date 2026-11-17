from __future__ import annotations

import pytest

from django_spec_agents.policy.approvals.answered_by_metadata import AnsweredByMetadata
from django_spec_agents.policy.approvals.granted_approvals import GrantedApprovals
from django_spec_agents.policy.approvals.types.never_auto_approved import NEVER_AUTO_APPROVED
from tests.policy.approvals.approval_runs import gated_tool, parked, returns, run_calls


async def test_a_granted_call_runs_in_the_same_run_marked_with_the_grant() -> None:
    result = await run_calls(
        [gated_tool("delete_order")],
        [GrantedApprovals(["delete_order"], "routine:7"), AnsweredByMetadata()],
        call=["delete_order"],
    )

    assert parked(result) == []
    part = returns(result)["delete_order"]
    assert part.content == "delete_order 0"
    assert part.metadata == {"answered_by": "routine:7"}


async def test_a_call_outside_the_grant_parks() -> None:
    result = await run_calls(
        [gated_tool("delete_order"), gated_tool("refund_order")],
        [GrantedApprovals(["delete_order"], "routine:7"), AnsweredByMetadata()],
        call=["delete_order", "refund_order"],
    )

    assert parked(result) == ["refund_order"]


@pytest.mark.parametrize("name", sorted(NEVER_AUTO_APPROVED))
async def test_a_grant_naming_an_excluded_tool_still_parks_it(name: str) -> None:
    result = await run_calls(
        [gated_tool(name)], [GrantedApprovals([name], "routine:7")], call=[name]
    )

    assert parked(result) == [name]
