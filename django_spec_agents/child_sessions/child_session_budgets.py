from __future__ import annotations

from typing import Any

from pydantic_ai.capabilities import AbstractCapability

from django_spec_agents.child_sessions.types.child_session_spend import ChildSessionSpend
from django_spec_agents.persistence.types.thread_ref import ThreadRef


def child_session_budgets(
    spend: ChildSessionSpend, root: ThreadRef, owner: str
) -> list[AbstractCapability[Any]]:
    """The tree and day ``SpendLimits`` a child under ``root`` carries; needs ``[harness]``."""
    from pydantic_ai_harness.spend_limits import Budget, SpendLimits

    tree = f"{root.scope or ''}:{root.thread_id}"
    return [
        SpendLimits(
            budgets=[
                Budget(
                    name="child_sessions_tree",
                    usd=spend.tree_usd,
                    window="total",
                    scope=lambda _: tree,
                )
            ],
            store=spend.store,
            id="child_sessions_tree_spend",
        ),
        SpendLimits(
            budgets=[
                Budget(
                    name="child_sessions_day",
                    usd=spend.day_usd,
                    window="day",
                    scope=lambda _: owner,
                )
            ],
            store=spend.store,
            id="child_sessions_day_spend",
        ),
    ]


__all__ = ["child_session_budgets"]
