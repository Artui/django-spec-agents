from __future__ import annotations

from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering, HandleDeferredToolCalls
from pydantic_ai.tools import (
    DeferredToolRequests,
    DeferredToolResults,
    ToolDefinition,
    matches_tool_selector,
)

from django_spec_agents.policy.approvals.granted_approvals import GrantedApprovals
from django_spec_agents.policy.approvals.types.approval_reviewer_config import (
    ApprovalReviewerConfig,
)
from django_spec_agents.policy.approvals.types.never_auto_approved import NEVER_AUTO_APPROVED
from django_spec_agents.policy.approvals.utils import approved_by, model_label


class ReviewedApprovals(AbstractCapability[Any]):
    """pydantic-ai-harness's ``ApprovalReviewer``, marking what it approves ``reviewer:<model>``.

    Built from an [`ApprovalReviewerConfig`][django_spec_agents.ApprovalReviewerConfig];
    needs the ``[harness]`` extra. A ``no`` approves the call, which runs in the same run;
    ``yes``, ``unsure`` and a failed review leave it unresolved. Tools in
    [`NEVER_AUTO_APPROVED`][django_spec_agents.NEVER_AUTO_APPROVED] are never reviewed,
    under a name list or ``'all'``. It sorts after
    [`GrantedApprovals`][django_spec_agents.GrantedApprovals] and before any
    ``HandleDeferredToolCalls``, whatever order the capabilities are listed in.
    """

    def __init__(self, config: ApprovalReviewerConfig) -> None:
        from pydantic_ai_harness.approval_reviewer import ApprovalReviewer

        super().__init__()
        self.config = config
        self.answered_by = f"reviewer:{model_label(config.model)}"
        self.reviewer: Any = ApprovalReviewer(
            config.model, question=config.question, tools=self._selects
        )

    def get_ordering(self) -> CapabilityOrdering:
        """A grant answers first; a project's own handler after."""
        return CapabilityOrdering(wrapped_by=[GrantedApprovals], wraps=[HandleDeferredToolCalls])

    async def handle_deferred_tool_calls(
        self, ctx: RunContext[Any], *, requests: DeferredToolRequests
    ) -> DeferredToolResults | None:
        """Approve the calls the reviewer clears; leave every other request unresolved."""
        reviewed = await self.reviewer.handle_deferred_tool_calls(ctx, requests=requests)
        return None if reviewed is None else approved_by(reviewed.approvals, self.answered_by)

    async def _selects(self, ctx: RunContext[Any], tool_def: ToolDefinition) -> bool:
        if tool_def.name in NEVER_AUTO_APPROVED:
            return False
        return await matches_tool_selector(self.config.tools, ctx, tool_def)


__all__ = ["ReviewedApprovals"]
