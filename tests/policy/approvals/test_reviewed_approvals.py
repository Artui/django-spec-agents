from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.models.test import TestModel

from django_spec_agents.policy.approvals.answered_by_metadata import AnsweredByMetadata
from django_spec_agents.policy.approvals.granted_approvals import GrantedApprovals
from django_spec_agents.policy.approvals.reviewed_approvals import ReviewedApprovals
from django_spec_agents.policy.approvals.types.approval_reviewer_config import (
    ApprovalReviewerConfig,
)
from django_spec_agents.policy.approvals.types.never_auto_approved import NEVER_AUTO_APPROVED
from tests.policy.approvals.approval_runs import (
    Reviewer,
    gated_tool,
    parked,
    returns,
    run_calls,
)


def _reviewed(reviewer: Reviewer, tools: Any = ("delete_order",)) -> ReviewedApprovals:
    config = ApprovalReviewerConfig(model=reviewer.model(), question="Risky?", tools=tools)
    return ReviewedApprovals(config)


async def test_a_reviewer_no_runs_the_call_marked_with_the_reviewer() -> None:
    reviewer = Reviewer("no")
    result = await run_calls(
        [gated_tool("delete_order")],
        [_reviewed(reviewer), AnsweredByMetadata()],
        call=["delete_order"],
    )

    assert parked(result) == []
    assert returns(result)["delete_order"].metadata == {"answered_by": "reviewer:function:judge"}
    assert len(reviewer.calls) == 1


@pytest.mark.parametrize("answer", ["yes", "unsure", None])
async def test_a_reviewer_yes_unsure_or_failure_parks_the_call(answer: str | None) -> None:
    reviewer = Reviewer(answer)
    result = await run_calls(
        [gated_tool("delete_order")], [_reviewed(reviewer)], call=["delete_order"]
    )

    assert parked(result) == ["delete_order"]
    assert len(reviewer.calls) == 1


async def test_a_tool_off_the_list_is_not_reviewed() -> None:
    reviewer = Reviewer("no")
    result = await run_calls(
        [gated_tool("refund_order")], [_reviewed(reviewer)], call=["refund_order"]
    )

    assert parked(result) == ["refund_order"]
    assert reviewer.calls == []


@pytest.mark.parametrize("name", sorted(NEVER_AUTO_APPROVED))
async def test_an_excluded_tool_on_the_list_parks_unreviewed(name: str) -> None:
    reviewer = Reviewer("no")
    result = await run_calls([gated_tool(name)], [_reviewed(reviewer, [name])], call=[name])

    assert parked(result) == [name]
    assert reviewer.calls == []


@pytest.mark.parametrize("name", sorted(NEVER_AUTO_APPROVED))
async def test_an_excluded_tool_under_all_parks_unreviewed(name: str) -> None:
    reviewer = Reviewer("no")
    result = await run_calls([gated_tool(name)], [_reviewed(reviewer, "all")], call=[name])

    assert parked(result) == [name]
    assert reviewer.calls == []


async def test_all_reviews_a_tool_no_list_names() -> None:
    reviewer = Reviewer("no")
    result = await run_calls(
        [gated_tool("refund_order")], [_reviewed(reviewer, "all")], call=["refund_order"]
    )

    assert parked(result) == []


async def test_a_grant_answers_before_the_reviewer_whatever_the_list_order() -> None:
    reviewer = Reviewer("no")
    result = await run_calls(
        [gated_tool("delete_order")],
        [
            _reviewed(reviewer),
            GrantedApprovals(["delete_order"], "routine:7"),
            AnsweredByMetadata(),
        ],
        call=["delete_order"],
    )

    assert returns(result)["delete_order"].metadata == {"answered_by": "routine:7"}
    assert reviewer.calls == []


def test_the_mark_is_a_model_string_as_given_or_a_models_system_and_name() -> None:
    config = ApprovalReviewerConfig(model=TestModel(), question="Risky?", tools="all")
    named = ApprovalReviewerConfig(model="test", question="Risky?", tools="all")

    assert ReviewedApprovals(config).answered_by == "reviewer:test:test"
    assert ReviewedApprovals(named).answered_by == "reviewer:test"
