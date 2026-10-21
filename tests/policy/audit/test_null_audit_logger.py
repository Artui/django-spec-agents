from __future__ import annotations

from django_spec_agents.policy.audit.null_audit_logger import NullAuditLogger
from django_spec_agents.policy.audit.types.audit_event import AuditEvent
from django_spec_agents.policy.audit.types.audit_logger import AuditLogger


def test_null_audit_logger_drops_events() -> None:
    logger = NullAuditLogger()
    event = AuditEvent(tool_name="x", arguments_repr="{}", duration_ms=0.5, success=True)
    assert logger.record(event) is None


def test_null_logger_satisfies_protocol() -> None:
    assert isinstance(NullAuditLogger(), AuditLogger)
