# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] — 2026-10-31

Requires Python 3.11 and pydantic-ai 2.58, as the rest of the family now does.

### Changed

- **Python 3.10 is dropped.** `requires-python` is `>=3.11`, the 3.10 classifier
  and its CI jobs are gone, and the lint target is Python 3.11.
- **`pydantic-ai-slim` is floored at 2.58**, in the core install and in the
  `[anthropic]`, `[openai]` and `[google]` extras, where the extras were at 2.16
  and 2.33. pydantic-ai 2.58.0 fixes GHSA-5pfm-2g3g-gh85, an advisory in its
  AG-UI adapter. This package does not serve that adapter (django-ag-ui does),
  so the floor does not by itself fix anything here: it lets the lock leave
  pydantic-ai 2.54.0, the last release that supports Python 3.10, and keeps the
  Python line in step with it. The lock now resolves pydantic-ai 2.61.0.
- Code, comments and tests that carried Python 3.10 or pydantic-ai releases
  before 2.55 are removed: the `asyncio.TimeoutError` alias in the dispatch
  timeout handler, a `NotRequired` test row that only differed on 3.10, and the
  audit tests' and documentation's account of how hooks ran before pydantic-ai
  2.54. Behaviour is unchanged.

## [0.4.1] — 2026-10-26

Runs on django-service-specs 0.16.0, as 0.4.0 does. A toolset binding a
reflected read named `request` or `view` through `tool_server_arguments` now
fails when it is built, where before the binding was dropped on every call.

### Fixed

- A required field of a service's Validator, beside its `input_data=` provider,
  is advertised and not required when that provider fills or may fill it,
  as a read beside a `kwargs=` provider already was. Beside a provider that
  returns no `TypedDict` every field is optional in the tool's schema; beside a
  typed one, the keys whose value admits `UnsetType` are. Dispatch never
  demanded such a field of the model, so the schema told it to send values the
  server supplies. A field nobody fills is still handed back to the model,
  by the kernel before the Validator runs, and a lookup parameter stays
  required. The rows of a `many` service are not relaxed.
- A `tool_server_arguments` binding on a reflected read named `request` or
  `view` is refused when the toolset is built, naming the tool, as a binding on
  a name a registered pool seed fills already was. Every call registers both
  names as seeds from the run's context, so the seed filled the read on every
  call and the binding was dropped without a word: the selector got `None` and
  nothing was logged.
- A seed `call_seeds` returns under a name `tool_server_arguments` binds or
  withholds for that tool is refused at the call with `ImproperlyConfigured`
  naming the tool. It used to win silently: the selector was handed the seed's
  value, the server's bound value was dropped, a withheld read was filled anyway,
  and nothing was logged. The same seed registered on the mount's `pool_seeds`
  was already refused when the toolset was built; a per-call seed now answers to
  the same check. A bound Validator field with a seed of the same name still
  works as it does on the mount: the field takes the bound value and the service
  parameter the seed.

## [0.4.0] — 2026-10-26

Runs on django-service-specs 0.16.0. A project with a read, shape, `many` list
or Validator field named `shapes` must rename it before upgrading, since the
kernel now reserves that name; the advertised tool schemas are unchanged.

### Added

- A selector, provider or service can read the call's shape values by declaring
  `shapes`, which the kernel hands it as a read-only mapping (empty on a spec
  declaring none). A value both halves need, as django-restql's `query` is for
  eager loading and field selection, can now be one shape rather than a read
  relayed through `call_context`; the relay remains for a value that is a read.
- A presenter overriding the kernel's `Presenter.presented_rows(values,
  context)` sees a whole page at once, for a lookup batched over its rows. The
  toolset slices a list before it renders, so it is called once per page served,
  with exactly that page's rows.

### Changed

- **Breaking, passed through from the kernel:** the `[specs]` floor is
  django-service-specs 0.16.0, which reserves `shapes` as the name of that seed.
  A read, shape or `many` list named `shapes` is now refused when the spec is
  declared, and a Validator field of that name when the toolset is built, so
  rename it for what it holds. The same floor moves the `return_schema` of a
  spec declaring `shapes=`, which requires no row field at any depth, to the
  kernel's `spec_output_schema` instead of a rewrite here; the advertised schema
  is unchanged.

## [0.3.0] — 2026-10-26

A tool's advertised schemas now say what dispatch holds a call to, which
changes them for two kinds of spec: one whose `kwargs=` provider has no
`TypedDict` return, and one declaring `shapes=`. Read Changed before upgrading
a project with either.

### Added

- Every call's context carries `transport`, set to `"agent"`, beside `action`.
  A provider, a selector, a permission check or a presenter declaring
  `call_context` reads it to tell a call an agent made from one over HTTP,
  which the request alone no longer says once the run carries the request it
  arrived on. A run's own `transport` entry is replaced, as its `action` is,
  and an affordance condition asked while the tools are listed sees the same
  value.
- The quickstart shows how one read reaches both the selector and the
  presenter, as django-restql's `query` drives eager loading and field
  selection: the selector takes it and writes it to the call's context, which
  the presenter is handed on each row of the page.

### Changed

- A tool's `parameters_json_schema` no longer lists in `required` a read that a
  `kwargs=` provider of the same seat may fill without saying which keys it
  fills: one with no return annotation, one annotated a plain `dict` (or
  `dict[str, Any]`), a lambda, or one whose annotation does not resolve. Such a
  read is still advertised. The call was already served without it, since the
  kernel's `server_owned_keys` asks a transport to demand neither such a read
  nor a key a provider may decline, so the schema promised the model a
  requirement the call never held it to. A call leaving the read out when the
  provider does not fill it either is handed back to the model, naming the
  read, as before; that now costs a retry round where the schema used to
  prevent it. To keep the reads required, annotate the provider's return: a
  `TypedDict` naming the keys it fills, or, for one that only reads and fills
  nothing, an empty `TypedDict` declared at module level.
- A spec declaring `shapes=` no longer lists a row's fields in `required` in the
  tool's `return_schema`, at any depth. A shape reaches the presenter alone, and
  one that selects fields leaves keys out, so the model was promised fields a
  selection drops. The `affordances` key each row carries, and a list tool's
  page envelope, stay required. A spec declaring no shapes is described as
  before.

## [0.2.1] — 2026-10-26

### Fixed

- `tool_calling_model` and `streaming_tool_calling_model` in
  `django_spec_agents.testing` call the tool again with `retry_args` on
  pydantic-ai 2.60 and later. From 2.60 a tool's `ModelRetry`, or arguments that
  failed validation, is recorded as the call's own `ToolReturnPart` marked
  `outcome="retried"` rather than as a `RetryPromptPart`, and the doubles read
  that as an answer and ended the run on the failed attempt. Both shapes are
  read now.

## [0.2.0] — 2026-10-23

`spec_tools` serves django-service-specs specs, the kernel's `ServiceSpec` and
`SelectorSpec`, and imports nothing from Django REST Framework or any other
spec library. Each entry below describes what a kernel-declared spec sees. A
project moving from `django-pydantic-agent` or `djangorestframework-pydantic-ai`
starts at [Moving from the old packages](https://artui.github.io/django-spec-agents/moving/).

### Added

- `AgentDeps.call_context`, one dict per run. Each tool call is handed its own
  copy, with `action` set to the tool's name and `request`, `view` and `format`
  set to `None` where the run put none, so what a stage writes during one call
  does not reach the next. A deps type without the field gets a fresh context
  per call.
- `SpecToolset.call_seeds(*, ctx, tool_name)`, an overridable hook returning the
  call's own seeds, name to resolver, registered over the toolset's
  `pool_seeds`. It is also asked once per conditioned tool when the tools are
  listed. A name the toolset's `pool_seeds` registers too is refused with
  `ValueError`, and a seed for a read the spec declares in `reads=` with
  `ImproperlyConfigured`. A parameter the hook fills that is read off the
  callable's signature is served from the seed, and a value the model sends
  for it is replaced; leave the name out of the spec's reads to keep it out of
  the schema as well.
- `SpecToolset` and `SpecCapability` serve the kernel's specs, in a mapping or a
  `SpecRegistry`. Every spec is read through `kernel_spec`, so what a tool
  advertises, fills and refuses is the compiled spec's. A read declared
  `nullable` keeps a `null` the model sends as its value.
- The toolset reads an optional `unsendable` attribute on a spec's Validator,
  a collection of field names the Validator requires and fills itself, so no
  call can send them. It refuses at build, with `ImproperlyConfigured` naming
  the tool and the field, a non-partial service whose Validator lists one. A
  Validator without the attribute names none, and a partial service is not
  refused.

### Removed

- **Breaking:** the `json_schema_registry=` keyword of `SpecToolset` and
  `SpecCapability`. A tool's input schema is the kernel's `spec_input_schema`
  of the compiled spec and its return schema the kernel's
  `spec_output_schema`, with no registry laid over either. A field's schema is
  whatever the spec compiles for it, and a `filter_set`'s filter is described
  as the kernel derives it from the filter set: a `NumberFilter` subclass is a
  decimal string, whatever type a project meant by it. A toolset-wide rule
  has no replacement; describe the field on the spec instead.
- **Breaking:** the `[specs]` extra installs `django-service-specs` alone, at
  `>=0.15.0`. `spec_tools` imports nothing from Django REST Framework or
  djangorestframework-services, and a djangorestframework-services spec is no
  longer a supported source. Declare each spec on the kernel: a read is a
  `Parameter` in `reads=`, a read-shaping argument one in `shapes=`, and a
  field's audience a `FieldMarking` on the presenter's output.
- The `[filter]` extra. Nothing in this package imports django-filter: a
  project declaring a `filter_set` has it installed already, and
  `django-service-specs[filter]` declares it for the kernel's own use. A
  project that relied on the extra to install django-filter declares
  `django-filter` directly, or uses the kernel's `django-service-specs[filter]`.
- `SpecToolset.build_context` and `SpecToolset.output_extras`, and the
  `http_request=`, `get_http_request=` and `host=` keywords of `SpecToolset` and
  `SpecCapability`. Each named a request or view the toolset built for a call,
  and the toolset builds none now:
  - a `build_context` override becomes a `call_seeds` override; a value a
    callable or a permission check reads is a seed it returns, and the tool's
    name is in the call's context under `action`;
  - the request a run arrived on is `call_context["request"]`, put there by
    whoever starts the run, where `http_request=` and `get_http_request=` named
    it. A presenter can read it there to make a URL absolute, for example with
    `request.build_absolute_uri`, where `host=` named one; the kernel's
    presenters do not do this;
  - `output_extras` goes with the `page`, `result` and `instance` keys it
    handed output-context providers: the kernel presents a result row by row.

### Changed

- `render_output` is `render_output(spec, result, *, ctx, projection)` and
  defaults to the kernel's `present_for_audience`. An override that wants the
  unprojected payload calls the kernel's `present(spec, result)`.
- What a call carries besides its arguments. A permission check is asked
  through the kernel's `PermissionCheck`: a keyword `arguments` is handed the
  call's arguments, `call_context` the call's context and a keyword named after
  a registered seed that seed's value. A selector or provider declaring
  `request` or `view` receives `None` unless the run puts one in `call_context`
  or a `call_seeds` override returns one.
- A read a selector or provider takes, by name or through `**kwargs`, is that
  callable's argument only; a read no callable takes is one of the spec's
  `shapes` and reaches the presenter. The tool's schema describes a taken read
  as an argument rather than as shaping each item, and the instructions'
  read-shaping line names only the shapes. An explicit `null` under a read or
  shape that is not `nullable` is treated as omitted, and its declared
  default, when it has one, answers.
- The failure policy's default `reraise` set now includes the kernel's
  `NotPermitted` and `PrincipalUnavailable`, so a refusal from the kernel's
  dispatch, including a deactivated principal, ends the run as Django's
  `PermissionDenied` does, rather than reaching the model as a failed tool
  call.
- The instructions block no longer names Django REST Framework.
  `AgentConventions.base` now opens "The following tools call this
  application's services and selectors." Every deployed agent reads this line
  on every step, so a deployment that pinned the old text will see the
  difference, and an override derived from the default, such as
  `AgentConventions().base.replace("Django REST Framework", ...)`, now replaces
  nothing.
- `SpecToolset` runs each call through the kernel's `dispatch` on the compiled
  spec. What a model and a run see of that:
  - An argument no parameter declares is refused in the kernel's words, one
    line per key: `note: Unknown argument.` where it was
    `Unexpected argument(s): 'note'.`
  - A call wrong in several places is handed one retry naming each, a line
    apiece: a service call sending a mistyped lookup value and leaving out an
    input field hears `pk: Expected a string.` and
    `name: This field is required.` together, where it heard about `pk` alone
    and about the field on its next turn.
  - A call's arguments are checked before its permissions, so a malformed call
    to a tool its caller may not use is handed back as a retry naming the
    argument, where it used to be refused with `PermissionDenied`.
  - A permission refusal is the kernel's `NotPermitted`, and a deactivated
    principal its `PrincipalUnavailable`; Django's own `PermissionDenied`,
    raised bare by a service, is the same refusal. Every refusal is logged at
    `WARNING` naming the tool.
  - A spec with no permissions, served by a toolset built with
    `require_permissions=False`, still runs unchecked, and only that spec: a
    spec that declares a check is asked it on every call.
  - **Breaking:** a filter-owned `ordering` is an array of sort names, and one
    sent as a string is handed back as a retry, `ordering: Expected an array.`,
    where it used to be split on commas. That includes an empty string, which
    used to mean no sort: leave the argument out instead.
  - Under `UnknownArguments.IGNORE`, an argument sent beside a many-spec's
    list is now dropped rather than refused, as any undeclared key is, so a
    deployed agent that relied on the refusal no longer gets it. `REJECT`
    still refuses it.
  - A registered pool seed fills a selector's, a target lookup's or a spread
    service's parameter of its name. When the spec declares that parameter,
    the model's own argument of the name is dropped before dispatch. A
    Validator field named like a registered seed is the model's to send: it is
    validated into `data`, and the service's parameter of that name still
    receives the seed's value. On a many-spec each row's field is validated
    the same way, where each row used to be refused as missing it.
  - The kernel closes a call's argument set, so under the default
    `UnknownArguments.REJECT` these are now refused where they used to be
    dropped or served: every key sent to a selector taking a bare `**kwargs`,
    which declares no name; an argument named like a registered seed that the
    spec never declares; and an argument named like one of the dispatcher's
    own, such as `user`, which the dispatcher fills where a selector or
    service reads it and no schema advertises as a parameter. `IGNORE` drops
    each of them, and none reaches the callable.
- `SpecToolset` reads each tool's input schema off the kernel's
  `spec_input_schema`, less the names the server fills, and its return schema
  off the kernel's `spec_output_schema`. What a model sees of that:
  - The input schema states `additionalProperties: false` under
    `UnknownArguments.REJECT` only, at the top and on each row of a many-spec's
    list, and nothing under `IGNORE`, which serves a call carrying an
    undeclared key.
  - A filter-owned `ordering` is advertised as an array of the filter's sort
    names, at most 1000 of them.
  - A toolset built from `registry.specs()` advertises the spec's reads, as one
    built from the registry does.
  - A `NumberFilter` is advertised as the decimal string its field parses, not
    as a JSON number.
  - A parameter admitting `None` is one node whose `type` lists `"null"`, where
    it was a choice of two nodes.
  - `required` lists names in the kernel's order, which puts the spec's reads
    before the list a many-spec takes, where the reads used to come last.
  - A read's declared default reaches the callable when a call leaves the read
    out, from a registry or a mapping.
  - A spec's `metadata["json_schema"]` fragments are merged over the kernel's
    schemas key by key, `input` over the input schema and `output` over the
    return schema.
- A read declared `required=True` with a `default` is no longer refused when
  the toolset is built; the default answers a call that leaves it out, as the
  kernel reads it.
- **Breaking:** a call is answered by the kernel's refusals, and Django's own
  `PermissionDenied`, only. Its
  `NotPermitted` and `PrincipalUnavailable`, and Django's own
  `PermissionDenied`, are a denial, logged and re-raised, and its `InvalidArguments` and `ServiceValidationError`, raised
  while dispatching or while rendering a read-shaping argument the model sent,
  are handed back to the model as a retry. Django REST Framework's
  `PermissionDenied` and `ValidationError` are neither any longer: one raised
  during a call escapes it unchanged, so a callable that raises them has to
  raise the kernel's instead.
- Two more declarations are refused when the toolset is built, with
  `ImproperlyConfigured` naming the tool, failing the whole toolset where its
  other tools used to build:
  - a Validator field sharing the name of a parameter of the service's
    target lookup, which no call can serve;
  - a `metadata["json_schema"]` whose `output` phase is not a mapping. A key
    naming no phase, and an `input` phase that is not a mapping, were refused
    in the same words already.
- **Breaking:** the default `id` of `SpecToolset` and `SpecCapability` is
  `spec-tools`, where it was `drf-specs`. A project that keys anything on the
  id, such as a deferred capability's catalog entry or a log filter, passes
  `id="drf-specs"` to keep it.
- **Breaking:** a tool built from a bare mapping, such as
  `registry.specs()`, is checked as a registry-built one is: a read the spec
  declares under a registered pool seed's name, at the top level or on a
  service's target lookup, fails the whole toolset when it is built, with
  `ImproperlyConfigured` naming the tool, where the tool used to build and
  every call to it was refused. So does a shape under `page`, `limit` or
  `ordering`, on a list or a retrieve tool: a mapping used to build it.
  Rename the read. A registry and its mapping now build the same tools, and
  the instructions' read-shaping line names a mapping-built tool's shapes
  too.
- A value in `specs` that compiles to no spec raises `TypeError` in the
  kernel's words, prefixed with the tool's name, rather than the toolset's
  own.
- A refusal keyed under Django's `__all__` is read as a message about the
  whole input, without the key, as one under `non_field_errors` is.

### Known differences

- A `returns_many` service with no output selector that returns a single row
  fails with the presenter's error out of the call, where it was a
  configuration error naming what the service returned.
- A selector taking `**kwargs` beside a `filter_set` receives the FilterSet's
  values, `ordering` among them, in its catch-all; they belong to the filter
  step and used to be withheld.

## [0.1.0] — 2026-10-21

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
- The `[drf-mcp]` extra requires `djangorestframework-mcp-server>=0.53.1`, the
  release that requires `django-service-specs>=0.10.0`, so a selector raising
  `Http404` is a missing row on that route as it is through `spec_tools`.
- The enums and constants moved from `constants.py` to one module each under
  `django_spec_agents.types`; the root names are unchanged, and
  `django_spec_agents.constants` is gone.

### Fixed

- A registry tool whose name differs from its function's, through
  `@tool(name=...)` or a `ToolSpec` registered directly, is now offered to the
  model under its registered name. It was offered under the function's own name,
  so a destructive tool carrying such a name was never collected by `ToolGuard`
  and ran without approval under an enabled guard, and the audit record and tool
  catalog named it differently from each other. A `description=` override now
  reaches the model too; a tool with neither override is offered exactly what it
  was. **Anything that named such a tool by its function name must now use the
  registered name**: a `require_approval` or `exempt` entry, an approval prompt,
  and a query on the audit or step store's `tool_name`. A `require_approval`
  entry left on the function name no longer matches, so that tool runs without
  approval.

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

[Unreleased]: https://github.com/Artui/django-spec-agents/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/Artui/django-spec-agents/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/Artui/django-spec-agents/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/Artui/django-spec-agents/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Artui/django-spec-agents/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/Artui/django-spec-agents/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Artui/django-spec-agents/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Artui/django-spec-agents/compare/v0.0.0...v0.1.0
