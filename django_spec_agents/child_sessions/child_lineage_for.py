from __future__ import annotations

from typing import Any

from pydantic_ai.capabilities import AbstractCapability

from django_spec_agents.child_sessions.child_lineage import ChildLineage
from django_spec_agents.child_sessions.child_report import ChildReport
from django_spec_agents.child_sessions.child_session_budgets import child_session_budgets
from django_spec_agents.child_sessions.types.child_session_spend import ChildSessionSpend
from django_spec_agents.child_sessions.types.inbound_queue import InboundQueue
from django_spec_agents.persistence.types.conversation import Conversation


def child_lineage_for(
    conversation: Conversation,
    *,
    agent_id: str,
    owner: str,
    queue: InboundQueue,
    spend: ChildSessionSpend | None = None,
) -> list[AbstractCapability[Any]]:
    """The capabilities a run of a child's thread composes, rebuilt from its stored lineage.

    The same ones ``start_session`` gave its first run: the parent's tool bounds, the
    report on settle, and, with ``spend``, the tree and day budgets. ``agent_id`` is the
    child's mount and ``owner`` its user's id; a thread with no stored lineage gets none.
    """
    record, parent = conversation.lineage, conversation.parent
    if record is None or parent is None:
        return []
    capabilities: list[AbstractCapability[Any]] = [
        ChildLineage(record.root, parent, record.depth, record.tools, record.gated),
        ChildReport(parent, agent_id, queue),
    ]
    if spend is not None:
        capabilities.extend(child_session_budgets(spend, record.root, owner))
    return capabilities


__all__ = ["child_lineage_for"]
