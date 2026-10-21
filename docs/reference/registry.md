# Registry

Server-side tools and their derived schemas. See [Tools](../tools.md) for the
narrative.

## `ToolRegistry`

::: django_spec_agents.ToolRegistry

## `tool`

::: django_spec_agents.tool

## `ToolSpec`

::: django_spec_agents.ToolSpec

## `ToolBinding`

::: django_spec_agents.ToolBinding

## `build_input_schema`

::: django_spec_agents.build_input_schema

## `ToolCategory`

::: django_spec_agents.ToolCategory

## Schema and metadata keys

The `x-*` keys are JSON-Schema extensions that reach the **client** on a tool's
schema. `DESTRUCTIVE_METADATA_KEY` is different: it rides pydantic-ai's tool
metadata and is read **server-side** by the [tool guard](policy.md), which needs
destructiveness for tools whose flag doesn't come from the `@tool` registry.

::: django_spec_agents.constants
    options:
      members:
        - X_DESTRUCTIVE_KEY
        - X_CATEGORY_KEY
        - X_CONFIRM_KEY
        - X_SUMMARY_KEY
        - DESTRUCTIVE_METADATA_KEY
