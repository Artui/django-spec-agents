# Repo conventions for `django-spec-agents`

This file is the single source of truth for how to write code in this package.
Rules are non-negotiable unless flagged as a heuristic.

## What this package is

Pydantic-AI agents for Django, in two halves under one import path,
`django_spec_agents`.

**The agent host**, the root and every subpackage but `spec_tools`, is the
settings-agnostic substrate shared by Django's Pydantic-AI transports: `AgentConfig`
in, a built `pydantic_ai.Agent` (composed toolsets, audit, user resolution) out. It
needs Django and `pydantic-ai-slim` only. It provides:

- `AgentConfig` + the agent builder; model / model-settings / retries / provider
  resolution and the agent-factory escape hatch.
- A tool registry (`ToolRegistry`, `@tool`) with `destructive=` / `category=` /
  `confirm=` / `summary=` metadata and typed JSON-Schema derivation
  (`x-destructive` / `x-category` / `x-confirm` / `x-summary` — plain annotations
  any protocol can carry).
- Toolset / capability composition, including the optional `[drf-mcp]` bridge and
  `build_spec_capability`, which reaches `spec_tools` when `[specs]` is installed.
- The `AuditLogger` protocol (`Null` / `Logging`), `ToolGuard`, `ToolFailurePolicy`
  and the `get_user(request)` user-resolution hook.
- Storage **contracts** — `ConversationStore` / `AttachmentStore` protocols —
  plus the reference `contrib.store` models and stores. (`DefaultStepStore`
  ships too, but its protocol is `pydantic-ai-harness`'s; this package does
  not declare a `StepStore` protocol of its own.)
- `testing`, the run-loop test doubles, which import pydantic-ai only.

**`spec_tools`**, behind the `[specs]` extra, serves django-service-specs specs
as agent tools in process: `SpecToolset`, and `SpecCapability` over it. It reads
every spec through the kernel's `kernel_spec` and imports nothing from Django REST
Framework or any other spec library, so the extra installs the kernel alone. DRF
and drf-mcp's own spec library arrive only with the `[drf-mcp]` bridge.

The host was published as `django-pydantic-agent` and `spec_tools` as
`djangorestframework-pydantic-ai`.

## Layout

| Path | Holds |
| --- | --- |
| `agent/` | `AgentConfig`, `AgentDeps`, the builder, the model builder, the tool catalog and the attachment toolset |
| `registry/` | `ToolRegistry`, `@tool`, schema derivation |
| `policy/` | `audit/`, `guard/` and `failure/`: the capabilities composed around every tool call |
| `persistence/` | the store protocols, the session, model, scoped and null stores, and the memory namespaces |
| `integrations/` | the `[drf-mcp]` bridge, `build_spec_capability` and `resolve_spec_mapping` |
| `contrib/store/` | the opt-in store app: models, the one migration, reference stores, management commands |
| `types/` | the package-wide enums and constants, one per file |
| `spec_tools/` | the tool half, with its own `types/` |
| `testing.py`, `utils.py`, `version.py` | the test doubles, the request and authorization helpers, the version |

`django_spec_agents/__init__.py` re-exports the host's public surface and nothing
of `spec_tools`. `spec_tools/__init__.py` has its own `__all__`, three names:
`AgentConventions`, `SpecCapability` and `SpecToolset`. A consumer imports them
from `django_spec_agents.spec_tools`, and `AgentDeps` from the root.

## The boundary between the halves

- **The root never imports `spec_tools`**, nor anything only `[specs]` or
  `[drf-mcp]` installs (`django_service_specs`, `rest_framework`,
  `rest_framework_services`), nor `testing`. A host-only install, which is what
  django-admin-agent ships, would fail at import otherwise. The host reaches `spec_tools` in one place,
  `integrations/build_spec_capability.py`, through a function-local import under
  rule 4's optional-dependency exception. `integrations/__init__.py` stays empty:
  django-ag-ui patches the function as an attribute of that submodule, which a
  re-export would shadow, so the patch would stop taking effect.
- **`spec_tools` imports nothing from the host but `AgentDeps`, and never
  `contrib.store`.** It reads the acting user structurally, as `ctx.deps.user`, so
  a project's own deps type works as long as it has a `user`.
- **`spec_tools` imports nothing from Django REST Framework or drf-services**,
  eagerly or in a function body. It reads every spec through the kernel.

`tests/test_boundary.py` holds all three: each half in a fresh interpreter under
settings that list neither `rest_framework` nor the spec test app
(`tests/host_only_settings.py`, and `tests/host_only_store_settings.py` with the
store app), and import scans of every module under `spec_tools/`, for the host
names it reaches and for DRF and drf-services. Inside the
suite's own process the check would prove nothing, because pytest-django has
imported `rest_framework` by collection time. `make test-host-only`, and the
`host-only install` CI job that runs it, runs that file on an install with no
extra. `tests/test_lock.py` holds that `uv.lock` names neither old distribution.

## The store app

`django_spec_agents.contrib.store`, label **`django_spec_agents_store`**: its
tables are `django_spec_agents_store_<model>` and its named constraints and
indexes are named after the package. One migration, `0001_initial`, creates the
schema on a fresh database and renames one migrated through
`django-pydantic-agent`'s store in place;
`tests/contrib/store/test_upgrade_from_django_pydantic_agent.py` holds the
upgrade, on SQLite in the suite and on PostgreSQL in its own CI job. Never add a
second migration that re-derives names from the old label.

Some names keep their old spelling because their value lives outside the
database: the attachment storage alias, the session key, the audit and failure
logger names and the `upload_to` prefix. A rename of any of them is silent for a
consumer, so `tests/test_kept_spellings.py` pins each against its literal.

### The one rule that defines this package

**The core reads no Django settings.** There is no `DJANGO_*` lookup anywhere in
`django_spec_agents`. Its public input is an `AgentConfig` dataclass; each
transport owns its own settings namespace, builds an `AgentConfig`, and hands it
down.

This is what keeps the transports independent: `django-ag-ui` keeps reading
`DJANGO_AG_UI`, `django-a2a` reads its own namespace, and neither fights the other
for keys. A settings read added here would turn every consumer's upgrade into a
key migration. **If you need a value, put it on `AgentConfig`.**

**One recorded exception: `STORAGES[ATTACHMENT_STORAGE_ALIAS]`.** A
`FileField`'s storage is bound when the model class is constructed, before any
`AgentConfig` exists, so a constructor argument cannot reach it — `STORAGES` is
Django's own hook and the only one available. It is scoped so the rule's
rationale does not apply: the key is optional, its absence is the previous
behaviour exactly, so no consumer's upgrade becomes a key migration. Do not read
this as licence for a second one; anything reachable from `AgentConfig` belongs
there.

## What belongs here vs. in a transport

The litmus test: **if it maps agent output to a specific wire format, or serves a
specific frontend, it is a transport; everything upstream of "how do I speak to the
peer" is core.**

| Concern | Lands in |
| --- | --- |
| `AgentConfig`, model / settings / retries / provider resolution, agent-factory escape hatch | **core** |
| `ToolRegistry` + `@tool` + typed-schema derivation | **core** |
| External toolsets / capabilities; the `[drf-mcp]` bridge and `[specs]` composition | **core** (optional extras) |
| `get_user(request)` + the `AuditLogger` protocol | **core** |
| `ConversationStore` / `AttachmentStore` protocols, the `read_attachment` tool, `contrib.store` models and stores (incl. `DefaultStepStore`, satisfying harness's protocol) | **core** (storage contracts are transport-neutral) |
| An HTTP view, an SSE/streaming encoder, a wire adapter, `.urls` | **a transport** |
| Browser-facing sub-views (thread drawer, attachments, transcription, skills palette) | **a transport** |

The stores are the non-obvious call: they are *contracts* (core), while the HTTP
*views* over them are per-transport — a thread drawer is a browser REST surface,
whereas an agent-to-agent peer models the same history as tasks/contexts entirely
differently. Reasoning is *produced* here (a model-settings thinking config) but
*mapped to wire events* by the transport.

## Commands

| Target | What it does |
| --- | --- |
| `make init` | `uv sync --all-groups` + install pre-commit hooks |
| `make test` | pytest with 100% line+branch coverage gate |
| `make test-host-only` | the boundary tests on an install with no extra, in `build/host-only-venv` |
| `make lint` | `ruff check .` + `ty check django_spec_agents` |
| `make format` | `ruff format .` |
| `make docs-serve` | live-reload mkdocs at `localhost:8000` |
| `make docs-build` | `mkdocs build --strict` |
| `make release-bump VERSION=X.Y.Z` | rewrite `version.py` + promote `[Unreleased]` in CHANGELOG |
| `make release-publish` | end-to-end workstation release |

## Structural rules

1. **One exported class or function per file.** File name = `snake_case` of the symbol.
   `ToolRegistry` → `tool_registry.py`; `build_input_schema` → `build_input_schema.py`.
   Enums and constant-like module-level values follow the same rule, one per file under
   `types/`. **Exception:** Django `models.py`, because Django requires models to live
   there, as it requires each management command to be a class named `Command` in
   `management/commands/<command-name>.py`.
2. **Private helpers used in only one file** stay there with a leading `_`.
3. **Non-exported helpers shared across files** go into a sibling `utils.py`. Classes are
   allowed in `utils.py` if they are internal infrastructure.
4. **Top-level imports only.** No function-local / lazy imports unless a circular import is
   genuine and documented inline at the import site, **or** the dependency is optional —
   those imports go inside the function body.
5. **Full type annotations on every function and method signature.** `Any` is allowed only at
   Django/Pydantic-AI boundaries where the type genuinely is `Any`.
6. **`__init__.py` is the only re-export point.** Each `__init__.py` lists the public surface
   in `__all__`. Internal modules import from leaf paths, never from the package's `__init__`.
7. **Always `from __future__ import annotations`** at the top of any file with type
   annotations. Python 3.11+, so no PEP 695 `type` statements.
8. **Absolute imports only.** Imports are ordered stdlib → third-party → first-party
   (`django_spec_agents`). Within each block, alphabetical.
9. **NEVER use relative imports.** `from . import x`, `from .foo import bar`, any dotted-
   relative form is forbidden everywhere in the package, including `__init__.py`.
10. **Types and functionality live in separate sub-packages.** When a directory contains both
    type declarations (dataclasses, Protocols, frozen wire-shape records) and functionality
    (callables, registries, dispatch helpers), the types move into a `types/` sibling.

## API style rules

11. **Always dataclasses over `dict[str, Any]` for structured data.** Every payload,
    configuration record, and tool spec field is a `@dataclass` with explicit field types.
    `dict[str, Any]` survives only at genuine serialisation boundaries.
12. **Tool callables are typed.** Every registered tool declares typed parameters and a typed
    return — no `**kwargs: Any` escape hatches. The registry derives JSON Schema from
    signatures; an untyped tool breaks the schema.
13. **Collaborators are constructor arguments, never dotted paths.** There is no
    `import_string` in this package. A caller that can hold a live object passes one.

## No module-level or class-level mutable state

State lives on instances. Module-level constants (lookup tables, regexes, dispatch tables)
are fine — module-level **mutable** state is not.

- No module-level mutable singletons (registries, caches, "warned-once" flags).
- No class-level mutable attributes declared on the class body. Initialise mutables in
  `__init__`.

## Tests

- `make test` runs pytest with `--cov=django_spec_agents --cov-fail-under=100`
  (line + branch). Restructure rather than reach for `# pragma: no cover`.
- Test layout mirrors the source tree under `tests/`.
  `django_spec_agents/foo/bar.py` → `tests/foo/test_bar.py`. `tests/spec_tools/`
  mirrors `spec_tools/` and carries the models its specs serve,
  `tests/spec_tools/testapp/` (label `testapp`). The tests whose subject is the
  package as a whole sit at `tests/` root: the boundary, the lock, the kept
  spellings and the module paths django-ag-ui imports (`test_consumer_paths.py`).
- `tests/conftest_settings.py` is the Django settings module pytest uses (set via
  `DJANGO_SETTINGS_MODULE` in `pyproject.toml`): the store app, the spec test app
  whose models the `spec_tools` suites' kernel specs serve, and `rest_framework`,
  for the `[drf-mcp]` bridge's tests alone, so one run covers both halves.
- Async tests: `async def test_...` with pytest-asyncio (`asyncio_mode = "auto"`).
- A store that writes through `sync_to_async` needs
  `@pytest.mark.django_db(transaction=True)` — its ORM calls run on a different
  connection than a transaction-wrapped test would roll back.

## Lint and types

- `make lint` runs `ruff check .` + `ty check django_spec_agents`. CI fails on either.
- `ruff format` is the source of truth for layout.
- Pre-commit runs `make lint-fix`, `make format`, `make type-check`. Commits must be clean
  before push — never `--no-verify`.
- `ty` is scoped to `django_spec_agents/` only (not tests).

**Four of these rules are enforced, not remembered.** `ruff` carries the
future-annotations import (`isort.required-imports`, with `__init__.py` and
`migrations/` exempt) and the ban on every relative form including single-dot
(`ban-relative-imports = "all"` — `TID` alone defaults to `parents`, so
`from .foo import x` slipped through). A pygrep pre-commit hook carries the
mypy-pragma rule, scoped to the package because that is exactly what `ty`
checks: under `tests/` no checker reads either pragma, while an editor running
mypy or pyright does honour `# type: ignore`. The `UP040/046/047` ignores stop
`ruff --fix` rewriting code *into* the PEP 695 syntax these conventions forbid.
Change a rule here and change the config with it, or they drift apart again.


## Compatibility floor

| Component | Floor | Tested |
| --- | --- | --- |
| Python | 3.11 | 3.11, 3.12, 3.13, 3.14 |
| Django | 4.2 LTS | 4.2, 5.0, 5.1, 5.2, 6.0, 6.1 |
| Pydantic-AI | 2.58, the family's shared floor (2.55 requires Python 3.11); the extras share it | latest in matrix |
| `[specs]` | django-service-specs, at the floor `pyproject.toml` declares and says the reason for | latest in matrix; the floor in the `lowest declared versions` job |

## Branching

When working on a new feature or version bump, **ALWAYS** switch to a new branch first
(`git checkout -b feat/...` or `release/vX.Y.Z`) and push to that branch. Never commit
feature work or version bumps directly to `main`, and never push to `main` from the local
checkout — `main` only advances via merged PRs.

## Releases

Merge-to-main triggered. `.github/workflows/release.yml` runs on every push to `main`,
publishes to PyPI via OIDC trusted publishing when the version was bumped, tags it, and
deploys docs to `gh-pages`.

### Cutting a release

```bash
make release-bump VERSION=0.2.0
git diff
git commit -am "Release 0.2.0"
git push -u origin release/0.2.0
gh pr create
# Merge to main; release.yml fires on the merge commit.
```

The repository starts at `0.0.0` with a `v0.0.0` tag, so the first real release is
`make release-bump VERSION=0.1.0` like any other.

### One-time setup (manual)

1. **PyPI Trusted Publisher** — `Artui/django-spec-agents`, workflow `release.yml`,
   environment `pypi`. For a project with no releases yet this is a *pending* publisher.
2. **GitHub Environment** — create `pypi` (no secrets; OIDC).
3. **GitHub Pages** — branch `gh-pages` (created on first release with docs).
