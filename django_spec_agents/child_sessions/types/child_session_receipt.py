from __future__ import annotations

from dataclasses import dataclass

from django_spec_agents.child_sessions.types.receipt_note import RECEIPT_NOTE


@dataclass(frozen=True)
class ChildSessionReceipt:
    """What ``start_session`` returns as its tool result: the child's ``(agent_id, thread_id)``.

    The tool result is the part of a delegation that persists, so a client finds the
    child's thread here after a reload.
    """

    agent_id: str
    thread_id: str
    run_id: str
    note: str = RECEIPT_NOTE


__all__ = ["ChildSessionReceipt"]
