"""An ``AuditLogger`` that keeps every event it is handed, for the tests that read them."""

from __future__ import annotations

from django_spec_agents.policy.audit.types.audit_event import AuditEvent


class CapturingAuditLogger:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)
