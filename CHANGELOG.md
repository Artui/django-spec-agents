# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- One package for Pydantic-AI agents in Django, in two halves. The agent host was
  published as `django-pydantic-agent`
  ([changelog](https://github.com/Artui/django-pydantic-agent/blob/main/CHANGELOG.md));
  `django_spec_agents.spec_tools`, behind the `[specs]` extra, was published as
  `djangorestframework-pydantic-ai`
  ([changelog](https://github.com/Artui/djangorestframework-pydantic-ai/blob/main/CHANGELOG.md)).
  The package root never imports `spec_tools`, so the host installs and runs
  without the extra.
- `django_spec_agents.testing` holds the run-loop test doubles. It imports
  pydantic-ai only, and the package root does not import it. It replaces
  `spec_tools.testing`.

### Changed

- The store app's label is `django_spec_agents_store`, and its tables, named
  constraints and named indexes are named after it. One migration, `0001_initial`,
  creates them on a fresh database and, on one migrated through
  `django-pydantic-agent`'s store, renames its tables, named constraints, named
  indexes, PostgreSQL's `_like` index on the attachment file column, content types
  and migration records in place, so the upgrade is a single `migrate`. The other
  objects Django named itself keep their old names on PostgreSQL. A database short of
  that store's last migration is refused with a message naming it.
- `DESTRUCTIVE_METADATA_KEY` is `"django_spec_agents.destructive"`.
- The attachments toolset id is `django-spec-agents-attachments`.
- The `ImproperlyConfigured` raised when a model cannot be built, and the error
  `ToolFailurePolicy` logs when it converts a tool's failure, open with
  `django-spec-agents: `.
- The audit `ContextVar` is named `django_spec_agents.audit.execution`.
- The enums and constants moved from `constants.py` to one module each under
  `django_spec_agents.types`; the root names are unchanged, and
  `django_spec_agents.constants` is gone.

### Fixed

- A tool registered with `@tool(name=...)` is now offered to the model under
  that name. It was offered under the function's own name, so a destructive tool
  carrying a `name=` override was never collected by `ToolGuard` and ran without
  approval under an enabled guard, and the audit record and tool catalog named it
  differently from each other. A `description=` override now reaches the
  model too; a tool with neither override is offered exactly what it was.

### Removed

- `SpecToolset` and `SpecCapability` no longer take the mount-level channel keywords
  `query_params=`, `tool_query_params=`, `url_kwargs=` and `tool_url_kwargs=`;
  passing one raises `TypeError`. A spec declares the channels it reads with `reads=`
  on the spec itself, and a value the server supplies goes in
  `tool_server_arguments`.
- `django_spec_agents.spec_tools` exports `AgentConventions`, `SpecCapability` and
  `SpecToolset` only. `AgentDeps` comes from `django_spec_agents`, and `QueryParam`
  and `UrlKwarg` from `rest_framework_services`; the `spec_tools.types` modules for
  them are gone.

[Unreleased]: https://github.com/Artui/django-spec-agents/compare/v0.0.0...HEAD
