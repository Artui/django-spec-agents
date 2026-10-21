# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- The store app's label is `django_spec_agents_store`, and its tables, constraints and
  indexes are named after it. One migration, `0001_initial`, creates them on a fresh
  database and, on one migrated through `django-pydantic-agent`'s store, renames its
  tables, constraints, indexes, content types and migration records in place, so the
  upgrade is a single `migrate`. A database short of that store's last migration is
  refused with a message naming it.
- `DESTRUCTIVE_METADATA_KEY` is `"django_spec_agents.destructive"`.
- The attachments toolset id is `django-spec-agents-attachments`.
- Error messages raised by the package are prefixed `django-spec-agents: `.
- The audit `ContextVar` is named `django_spec_agents.audit.execution`.
- The enums and constants moved from `constants.py` to one module each under
  `django_spec_agents.types`; the root names are unchanged, and
  `django_spec_agents.constants` is gone.

[Unreleased]: https://github.com/Artui/django-spec-agents/compare/v0.0.0...HEAD
