from __future__ import annotations

from typing import Protocol, runtime_checkable

from django_spec_agents.policy.audit.types.audit_event import AuditEvent


@runtime_checkable
class AuditLogger(Protocol):
    """Sink for tool-invocation records."""

    def record(self, event: AuditEvent) -> None: ...


__all__ = ["AuditLogger"]
