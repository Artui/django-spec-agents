from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuditEvent:
    """A single tool invocation as seen by the audit logger."""

    tool_name: str
    arguments_repr: str
    duration_ms: float
    success: bool
    error: str | None = None
    result_size: int | None = None

    organization_id: str | None = None
    """Multi-tenant scope of the acting user. ``None`` at this layer; a custom
    [`AuditLogger`][django_spec_agents.AuditLogger] fills it from its own
    tenancy model."""

    target_type: str | None = None
    """Kind of domain object the call acted on. ``None`` at this layer, where
    tool arguments are domain-opaque; a sink that knows its tools can
    classify them."""

    target_id: str | None = None
    """Identifier of the acted-on object, paired with ``target_type``."""

    ip_address: str | None = None
    """Client IP of the request that drove the run, when the view knows it."""


__all__ = ["AuditEvent"]
