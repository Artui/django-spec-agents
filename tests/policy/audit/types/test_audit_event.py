from __future__ import annotations

import pytest

from django_spec_agents.policy.audit.types.audit_event import AuditEvent


def test_audit_event_is_frozen() -> None:
    event = AuditEvent(tool_name="x", arguments_repr="{}", duration_ms=1.0, success=True)
    assert event.error is None
    assert event.result_size is None
    with pytest.raises(AttributeError):
        event.tool_name = "y"  # type: ignore[misc]
