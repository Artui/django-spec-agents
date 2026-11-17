from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering, HandleDeferredToolCalls
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from django_spec_agents.policy.approvals.types.never_auto_approved import NEVER_AUTO_APPROVED
from django_spec_agents.policy.approvals.utils import approved_by


class GrantedApprovals(AbstractCapability[Any]):
    """Approves a fixed grant of tool names in the run, marked ``answered_by``.

    A routine's firing spec can pass ``preapproved_exempt(routine)`` with
    ``answered_by="routine:<id>"``: the guard still gates each call, and this answers it,
    so the call runs in the same run with the mark on its result and its audit record. A
    call of any other tool, an external call, and a tool in
    [`NEVER_AUTO_APPROVED`][django_spec_agents.NEVER_AUTO_APPROVED] are left to the next
    answerer, and then to the person. It sorts before
    [`ReviewedApprovals`][django_spec_agents.ReviewedApprovals], so a granted call costs
    no review.
    """

    def __init__(self, names: Iterable[str], answered_by: str) -> None:
        super().__init__()
        self.names = frozenset(names) - NEVER_AUTO_APPROVED
        self.answered_by = answered_by

    def get_ordering(self) -> CapabilityOrdering:
        """Answer before any project handler hands a call to a person."""
        return CapabilityOrdering(wraps=[HandleDeferredToolCalls])

    async def handle_deferred_tool_calls(
        self, ctx: RunContext[Any], *, requests: DeferredToolRequests
    ) -> DeferredToolResults | None:
        """Approve the granted calls; leave every other request unresolved."""
        granted = (call.tool_call_id for call in requests.approvals if call.tool_name in self.names)
        return approved_by(granted, self.answered_by)


__all__ = ["GrantedApprovals"]
