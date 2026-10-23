# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  `ValueError`, and a seed for a declared `UrlKwarg` read with
  `ImproperlyConfigured`. A name only the hook fills, and the spec does not mark
  as server-filled, is served from the seed, and a value the model sends for it
  is replaced, where the call used to be retried.

### Removed

- `SpecToolset.build_context` and `SpecToolset.output_extras`, and the
  `http_request=`, `get_http_request=` and `host=` keywords of `SpecToolset` and
  `SpecCapability`. Each named a DRF request or view the toolset built for a
  call, and the toolset builds none now:
  - a `build_context` override becomes a `call_seeds` override; a view a
    permission class reads is a `view` seed it returns, and the tool's name a
    permission class reads as `view.action` is in the call's context under
    `action`;
  - the request a run arrived on is `call_context["request"]`, put there by
    whoever starts the run, where `http_request=` and `get_http_request=` named
    it;
  - absolute file URLs follow the host of the run's request, where `host=`
    named one;
  - `output_extras` goes with the `page`, `result` and `instance` keys it
    handed output-context providers.

### Changed

- `render_output` is `render_output(spec, result, *, ctx, projection)` and
  defaults to the kernel's `present_for_audience`. An override that wants the
  unprojected payload calls the kernel's `present(spec, result)`.
- A `UrlKwarg` read reaches a provider or selector by its name, as an argument,
  never through `view.kwargs`, so a provider reading it off `view.kwargs` takes
  it by name instead. A callable declaring `request` or `view` receives `None`
  where the run put none.
- A `QueryParam` read that a selector or provider takes, by name or through
  `**kwargs`, is that callable's argument only, and no longer reaches the output
  serializer's query string. The tool's schema describes it as an argument
  rather than as shaping each item, and the instructions' read-shaping line no
  longer names it. An explicit `null` for any `QueryParam` read is treated as
  omitted, and a declared default is applied whether or not the read shapes the
  output.

- The `[specs]` extra requires `djangorestframework-services>=0.61.0` and
  `django-service-specs>=0.15.0`.
- The failure policy's default `reraise` set now includes the kernel's
  `NotPermitted` and `PrincipalUnavailable`, so a refusal from the kernel's
  dispatch, including a deactivated principal, ends the run as Django's and
  DRF's `PermissionDenied` do, rather than reaching the model as a failed tool
  call.
- The instructions block no longer names Django REST Framework.
  `AgentConventions.base` now opens "The following tools call this
  application's services and selectors." Every deployed agent reads this line
  on every step, so a deployment that pinned the old text will see the
  difference, and an override derived from the default, such as
  `AgentConventions().base.replace("Django REST Framework", ...)`, now replaces
  nothing.
- `SpecToolset` runs each call through the kernel's `dispatch` on the compiled
  spec, rather than through drf-services' `dispatch_spec`. What a model and a
  run see of that:
  - An argument no parameter declares is refused in the kernel's words, one
    line per key: `note: Unknown argument.` where it was
    `Unexpected argument(s): 'note'.`
  - A call's arguments are checked before its permissions, so a malformed call
    to a tool its caller may not use is handed back as a retry naming the
    argument, where it used to be refused with `PermissionDenied`.
  - A deactivated principal is refused with the kernel's own
    `PrincipalUnavailable`, which is not a DRF `PermissionDenied`, so code
    catching DRF's refusal around a run no longer catches it. A spec's
    permission refusal is drf-services' `NotPermitted`, which is both DRF's
    `PermissionDenied` and the kernel's `NotPermitted`, so a handler written
    against either catches it, and its message is unchanged. Every refusal is
    logged at `WARNING` naming the tool, the kernel's included.
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
    the model's own argument of the name is dropped before dispatch. An
    input serializer field named like a registered seed is the model's to
    send: it is validated into `data`, and the service's parameter of that
    name still receives the seed's value. On a many-spec each row's field is
    validated the same way, where each row used to be refused as missing it.
  - The kernel closes a call's argument set, so under the default
    `UnknownArguments.REJECT` these are now refused where they used to be
    dropped or served: every key sent to a selector taking a bare `**kwargs`,
    which declares no name; an argument named like a registered seed that the
    spec never declares; an argument of a name marked `NotClientInput`,
    whatever the selector's input set; and an argument named like one of the
    dispatcher's own, such as `user`, which the dispatcher fills where a
    selector or service reads it and no schema advertises as a parameter.
    `IGNORE` drops each of them, and none reaches the callable.
- `SpecToolset` reads each tool's input schema off the kernel's
  `spec_input_schema`, less the names the server fills, and its return schema
  off the kernel's `spec_output_schema`, rather than off drf-services'
  reflection. What a model sees of that:
  - The input schema states `additionalProperties: false` under
    `UnknownArguments.REJECT` only, at the top and on each row of a many-spec's
    list, and nothing under `IGNORE`, which serves a call carrying an
    undeclared key.
  - A filter-owned `ordering` is advertised as an array of the filter's sort
    names, at most 1000 of them.
  - A toolset built from `registry.specs()` advertises the spec's reads, as one
    built from the registry does, where it used to advertise none.
  - A `NumberFilter` is advertised as the decimal string its field parses, not
    as a JSON number.
  - A parameter admitting `None` is one node whose `type` lists `"null"`, where
    it was a choice of two nodes.
  - `required` lists names in the kernel's order, which puts the spec's reads
    before the list a many-spec takes, where the reads used to come last.
  - A read's declared default reaches the callable when a call leaves the read
    out, a `QueryParam` and a `UrlKwarg` alike, from a registry or a mapping.
  - A `null` sent under a `UrlKwarg` read is treated as omitted, as one under a
    `QueryParam` now is, so the read's default answers, where it was
    refused as `This field cannot be null.`
  - A spec's `metadata["json_schema"]` fragments are merged over the kernel's
    schemas key by key, `input` over the input schema and `output` over the
    return schema, as drf-services merged them.
- **Breaking:** an input serializer field the serializer requires under a name
  drf-services fills itself (`user`, `data`, `instance`, `collection`,
  `result`, `queryset`, `progress`, `call_context` or `serializer`), on a
  single service or on a `many=True` service's rows, now fails the whole
  toolset when it is built, with `ImproperlyConfigured` naming the tool and
  the field. 0.1.0 built the tool and served the call, the serializer
  receiving the model's value under that name; drf-services' compiled
  Validator declares no parameter for the name, so no call could be served
  now. Rename the field, or make it optional, and the tool builds.
- Two more declarations are refused when the toolset is built, with
  `ImproperlyConfigured` naming the tool, failing the whole toolset where its
  other tools used to build:
  - an input serializer field sharing the name of a parameter of the service's
    target lookup, which no call can serve;
  - a `metadata["json_schema"]` whose `output` phase is not a mapping, which
    drf-services refuses when it derives the output schema
    (`spec_to_json_schema(spec, phase="output")`). A key naming no phase, and
    an `input` phase that is not a mapping, were refused in the same words
    already.

### Known differences

- A `returns_many` service with no `selector` that returns a single row raises
  the output serializer's `AttributeError` out of the call, where it was a
  configuration error naming what the service returned.
- A selector taking `**kwargs` beside a `filter_set` receives the FilterSet's
  values, `ordering` among them, in its catch-all; they belong to the filter
  step and used to be withheld.
- A selector or provider that takes a `QueryParam` read through `**kwargs`
  receives it, and the output serializer no longer does, so django-restql's
  field selection under such a selector is not applied.
- A declared read that its callable takes off `request.query_params` or
  `view.kwargs` rather than by name is still accepted, and advertised, and
  reaches nothing: the call answers as if it
  had not been sent.

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

[Unreleased]: https://github.com/Artui/django-spec-agents/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Artui/django-spec-agents/compare/v0.0.0...v0.1.0
