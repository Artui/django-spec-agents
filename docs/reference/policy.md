# Policy

Audit, the destructive-tool gate and the tool-failure policy all ride the
Pydantic-AI capability seam. The first two are off by default; the third is on.
See [Policy](../policy.md) for the narrative.

## Audit

### `AuditLogger`

::: django_spec_agents.AuditLogger

### `AuditEvent`

::: django_spec_agents.AuditEvent

### `AuditCapability`

::: django_spec_agents.AuditCapability

### `NullAuditLogger`

::: django_spec_agents.NullAuditLogger

### `LoggingAuditLogger`

::: django_spec_agents.LoggingAuditLogger

## Tool guard

### `ToolGuardConfig`

::: django_spec_agents.ToolGuardConfig

### `ToolGuard`

::: django_spec_agents.ToolGuard

## Tool failure

### `ToolFailureConfig`

::: django_spec_agents.ToolFailureConfig

### `ToolFailurePolicy`

::: django_spec_agents.ToolFailurePolicy
