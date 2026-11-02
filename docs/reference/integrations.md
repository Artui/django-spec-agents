# Integrations

Optional bridges exposing declared specs as agent tools. Each is lazily
imported and lives under `django_spec_agents.integrations` rather than being
re-exported at the package root, so the base install pulls none of their
dependencies. See [Integrations](../integrations.md) for which to choose.

## Spec tools (`[specs]`)

### `build_spec_capability`

::: django_spec_agents.integrations.build_spec_capability.build_spec_capability

### `resolve_spec_mapping`

::: django_spec_agents.integrations.resolve_spec_mapping.resolve_spec_mapping

### `SpecSource`

::: django_spec_agents.integrations.types.spec_source.SpecSource

## MCP tools (`[mcp]`)

### `MCPToolset`

::: django_spec_agents.integrations.mcp.MCPToolset

## Deprecated MCP tools (`[drf-mcp]`)

Removed in the next minor; use `MCPToolset` and `[mcp]`.

### `DRFMCPToolset`

::: django_spec_agents.integrations.drf_mcp.DRFMCPToolset
