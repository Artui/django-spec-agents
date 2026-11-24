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

## In-run answers

### `ApprovalReviewerConfig`

::: django_spec_agents.ApprovalReviewerConfig

### `GrantedApprovals`

::: django_spec_agents.GrantedApprovals

### `ReviewedApprovals`

::: django_spec_agents.ReviewedApprovals

### `AnsweredByMetadata`

::: django_spec_agents.AnsweredByMetadata

### `NEVER_AUTO_APPROVED`

::: django_spec_agents.NEVER_AUTO_APPROVED

### `ANSWERED_BY_KEY`

::: django_spec_agents.ANSWERED_BY_KEY

## Tool failure

### `ToolFailureConfig`

::: django_spec_agents.ToolFailureConfig

### `ToolFailurePolicy`

::: django_spec_agents.ToolFailurePolicy

## Loops and spend

### `LoopDetectionConfig`

::: django_spec_agents.LoopDetectionConfig

### `SpendLimitsConfig`

::: django_spec_agents.SpendLimitsConfig

### `SpendBudget`

::: django_spec_agents.SpendBudget
