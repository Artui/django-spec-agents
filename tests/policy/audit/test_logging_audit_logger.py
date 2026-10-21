from __future__ import annotations

import logging
from typing import Any

from django_spec_agents.policy.audit.logging_audit_logger import LoggingAuditLogger
from django_spec_agents.policy.audit.types.audit_event import AuditEvent


def _event(**fields: Any) -> AuditEvent:
    defaults: dict[str, Any] = {
        "tool_name": "ping",
        "arguments_repr": "{}",
        "duration_ms": 1.0,
        "success": True,
    }
    return AuditEvent(**{**defaults, **fields})


def test_success_logs_at_info(caplog) -> None:  # noqa: ANN001
    logger = LoggingAuditLogger("test_logger.success")
    caplog.set_level(logging.INFO, logger="test_logger.success")
    logger.record(_event(arguments_repr='{"a": 1}', duration_ms=12.34, result_size=100))
    assert any(r.levelno == logging.INFO and "tool=ping" in r.message for r in caplog.records)


def test_failure_logs_at_warning(caplog) -> None:  # noqa: ANN001
    logger = LoggingAuditLogger("test_logger.failure")
    caplog.set_level(logging.WARNING, logger="test_logger.failure")
    logger.record(_event(tool_name="boom", success=False, error="ValueError: kaboom"))
    warning = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert "boom" in warning.message
    assert "ValueError" in warning.message


def test_context_fields_ride_the_log_line_when_set(caplog) -> None:  # noqa: ANN001
    logger = LoggingAuditLogger("test_logger.context")
    caplog.set_level(logging.INFO, logger="test_logger.context")
    logger.record(
        _event(
            result_size=2,
            organization_id="acme",
            target_type="order",
            target_id="42",
            ip_address="10.0.0.7",
        )
    )
    message = caplog.records[0].message
    assert "org=acme" in message
    assert "target=order:42" in message
    assert "ip=10.0.0.7" in message


def test_partial_target_renders_with_placeholder(caplog) -> None:  # noqa: ANN001
    logger = LoggingAuditLogger("test_logger.partial")
    caplog.set_level(logging.INFO, logger="test_logger.partial")
    logger.record(_event(target_id="42"))
    assert "target=?:42" in caplog.records[0].message
