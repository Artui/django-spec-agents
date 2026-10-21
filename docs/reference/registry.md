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

Each lives in its own module under `django_spec_agents.types` and is importable
from the package root.

::: django_spec_agents.types.x_destructive_key
    options:
      members:
        - X_DESTRUCTIVE_KEY

::: django_spec_agents.types.x_category_key
    options:
      members:
        - X_CATEGORY_KEY

::: django_spec_agents.types.x_confirm_key
    options:
      members:
        - X_CONFIRM_KEY

::: django_spec_agents.types.x_summary_key
    options:
      members:
        - X_SUMMARY_KEY

::: django_spec_agents.types.destructive_metadata_key
    options:
      members:
        - DESTRUCTIVE_METADATA_KEY
