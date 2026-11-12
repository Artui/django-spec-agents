# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **The API reference's docstrings are restored.** The long docstrings that the
  comment-budget cut to one line are back on the classes and functions the
  reference pages render.

## [0.13.0] — 2026-11-09

The floor on `django-service-specs` rises to 0.31.0, so a minor release. Nothing
the toolset serves changes but the schema of a labelled filter, which the kernel
now writes.

### Changed

- **A labelled filter's choices, an `OrderingFilter`'s among them, carry their
  labels into the tool's schema.** The kernel writes a labelled choice filter as
  `anyOf` of `const` and `title` where it wrote an `enum`, so the schema carries
  `{"const": "-created", "title": "Created (descending)"}` for each value the
  model may send. What a provider passes on varies: pydantic-ai's OpenAI
  transformer drops the titles and Google's turns the choices back into an
  `enum`. The values, and the `ModelRetry` for one outside them, are unchanged. A
  project asserting the old `enum` on a tool's schema changes its assertion.
- **The moving guide says what a view `call_seeds` returns owes a permission
  reading a route capture.** django-spec-rest's `DRFPermissionCheck` fills
  `view.kwargs` only on the view it builds when the call carries none, with the
  call's arguments its `route_reads` names. A returned view whose `kwargs` the
  override left empty holds no capture, so a class reading one refuses or raises
  `KeyError`. The answer is to return no view and name the capture in
  `route_reads`, at the price of a role `action` the view set.

### Fixed

- **A tool whose condition reads a seed that reads the call is listed.** The
  listing built each conditioned tool's pool without the call's context, so a
  registered seed declaring `call_context` or `request` failed to bind there with
  `TypeError`, and `get_tools` failed for the whole run, while the call itself
  answered. The pool is now the kernel's `seeded_pool`, under the context the
  call is built with.

## [0.12.0] — 2026-11-09

A tool's schema is built under the agent's stamp, so a field a Validator declares
to the agent transport is offered. The floor on `django-service-specs` rises to
0.30.0, so a minor release.

### Changed

- **A tool's schema is built under the agent's stamp, so a field a Validator
  declares to the `"agent"` transport is offered and required.** The schema
  passes `build_call_context(AGENT_TRANSPORT)` to the kernel's
  `spec_caller_input_schema`, once, when the toolset is constructed. A required
  field declared only to the agent was accepted when sent and never advertised,
  so every call the model made from the schema was refused for leaving it out. A
  field declared only to another transport is not offered, and a Validator that
  never opted in is described as before. A seed-named field the schema offers and
  the call's declaration drops is answered by the seed, as any seed-named argument
  is, and the model's value is dropped.
- **A field keyed on the run stays unoffered**, on its `request` or another entry
  of `deps.call_context`, since no run is in hand when the schema is built. A
  call sending it is still served, and the quickstart says what such a field
  should do instead.
- **A Validator declaring `call_context` is now read under the stamp when the
  toolset is built, so it guards `request`.** Reading
  `call_context["request"].user` past a `None` check passed and now raises there,
  its own error with a note naming the tool. A field declared only when handed
  `None` is no longer offered. A clash declared only to the agent is refused at
  the same point with `ImproperlyConfigured` naming the tool.
- **The `[specs]` extra and the dev group floor `django-service-specs` at 0.30.0,
  up from 0.29.0**, the release whose `spec_caller_input_schema` takes
  `call_context=`.
- **`AGENT_TRANSPORT` is listed in `spec_tools.types`**, beside `AgentConventions`,
  so the sub-package's `__all__` names every public value its modules define. It was
  already exported from `django_spec_agents.spec_tools`, and nothing there changes.
- **The `[mcp]` bridge's guide says what transport a bridged call carries**:
  whatever django-spec-mcp stamps, which is none at the 0.9.0 floor and `"mcp"` from
  0.10.0. A test holds each against the installed release, now that the lock names
  0.10.0 and the floor job runs 0.9.0, and another runs a call whose run is a
  `MappingProxyType`, which the stamp is written over without editing it.

## [0.11.0] — 2026-11-08

Every call's context is built through the kernel's `build_call_context`, and the
transport value it carries is exported. The floor on `django-service-specs` rises
to 0.29.0, so a minor release.

### Added

- **`AGENT_TRANSPORT`, the value every call's context carries under
  `transport`, is exported from `django_spec_agents.spec_tools`.** It is the
  plain string `"agent"`, this package's own: the kernel lists no transports.
  A callable written for several transports compares the string and imports
  nothing; the quickstart now says what a callable reads here and which entries
  of the run's context a consumer can and cannot override.

### Changed

- **The `[specs]` extra and the dev group floor `django-service-specs` at
  0.29.0, up from 0.26.0.** Every call's context, and the one the listing asks
  its conditions with, is built through the kernel's `build_call_context`, with
  the run's `call_context` as its `base`, so the stamp is the last write.
  Nothing a callable reads changes: `transport` is `"agent"` over any entry the
  run carries, `action` is the tool's name, `request`, `view` and `format` are
  `None` where the run put none, and each call is handed a fresh `dict`. A
  project pinning `django-service-specs` below 0.29.0 must raise it with this
  release. The floor on `django-spec-mcp` does not move.

## [0.10.1] — 2026-11-07

Two notes in the docs. No change to the package.

### Added

- **The page on running from a worker says a test whose tool writes needs
  `django_db(transaction=True)`.** Under the default `thread_sensitive=True` a
  tool call runs on asgiref's shared thread, whose connection the test's
  transaction does not reach. A write that succeeds commits and outlives the
  test. On PostgreSQL the tool cannot see the test's uncommitted rows, so a
  write pointing at one fails with `IntegrityError` and a read misses them; on
  SQLite, once the test has written, every tool write fails on the test's lock
  (`database is locked`, or `database table is locked` on Django's default
  in-memory test database). Rows an async test makes with the async ORM are
  made on that same thread, so they commit too. A test asserting only a refusal
  that reads nothing the test made, with its rows made by the sync ORM, can keep
  the plain marker. The danger box on `thread_sensitive=False` now points there,
  since the default behaves the same.
- **The quickstart says a service reads its actor from `user`, not
  `request.user`.** A run that puts no request in its context, as a worker's
  or a test's started with `AgentDeps(user=...)` alone does, hands a service
  `request=None`, so a service recording its actor from `request.user` records
  no one. The principal reaches every callable as the kernel's `user`.

## [0.10.0] — 2026-11-07

A list reads the same on both routes, as `{"items": [...]}`, and the MCP bridge
hands the model a scalar rather than its content blocks. A wire change for
`[specs]` users, so a minor release.

### Changed

- **A list result on the `[specs]` route is `{"items": [...]}`, not a bare
  array.** A service presenting a list -- a `LIST` output re-read, a
  `many=True` call, a `returns_many=True` return -- now hands the model its rows
  under `items`, and its `return_schema` is an object with that one required
  property holding the array. The value decides, as in django-spec-mcp, so any
  list or tuple a tool returns is wrapped the same way, a presenter-less
  service's and a `render_output` override's included. A `many` or `returns_many` service declaring
  `allow_none=True` that returns `None` is `{"items": []}`, so its schema no
  longer admits `null`. This is the shape django-spec-mcp 0.9.0 serves the same
  spec in, so one spec reads the same through `SpecToolset` and `MCPToolset`,
  and `items` is where a list selector's page already holds its rows. **A wire
  change**: a consumer reading such a tool's result as a list reads
  `result["items"]` now. A list selector's page, and every result that is neither a
  list nor a tuple, is unchanged. A `metadata["json_schema"]["output"]` fragment
  on such a spec now annotates the object, as it annotates a list selector's
  page, so a fragment that constrained the array (`minItems`, say) restates it
  under `properties.items`.
- **The `[mcp]` extra requires django-spec-mcp 0.9.0**, the release whose wire
  the bridge reads: a list under `items`, and a scalar with no
  `structuredContent`.

### Fixed

- **`MCPToolset` hands the model a scalar result, not its content blocks.**
  django-spec-mcp 0.9.0 serves a scalar (a presenter-less service returning a
  bool, number or string) as its JSON text block with no `structuredContent`,
  and the bridge handed the model `[{"type": "text", "text": "3"}]` where it
  meant `3`. A result without `structuredContent` is now read from its one text
  block, parsed as JSON and kept as the raw text when it is not JSON; that
  covers a tool registered with `include_structured_content=False` too, which
  handed over the blocks the same way on every release. Content that is not one
  text block, such as an image, is still handed on as its blocks.

## [0.9.0] — 2026-11-05

The wording a toolset derives names `total` when its pages carry it, through a
placeholder an overriding project can use too.

### Added

- **An `{envelope}` placeholder in `AgentConventions`, and the derived wording
  names `total` under `include_total`.** 0.8.0's `include_total=True` put
  `total` on every list tool's page and in its `limit` description, but the
  instructions' pagination line and the sentence on each read-shaping parameter
  still listed the envelope without it. Their defaults now write the page
  through `{envelope}`, which the toolset renders with `total` when the flag is
  on and without it when it is off; off, the instructions and every tool
  definition are byte-identical to 0.8.0. `pagination`, `read_shaping_on_pages`
  and `query_param_on_pages` accept it: in `pagination` it is the page written as
  an object, in the other two the envelope's keys, listed.
  **For a project that overrides those fields**, nothing changes until it opts
  in: text without the placeholder is said exactly as written, flag or not, and
  so does not mention `total`. Writing `{envelope}` where the text names the page
  hands keeping that list true to the toolset. `read_shaping_on_pages`'s default
  names no envelope key, so it reads the same either way. Code that formats a
  default itself, `AgentConventions().pagination.format(page_size=...)`, now
  passes `envelope=` too, or raises `KeyError`.

## [0.8.0] — 2026-11-04

A list tool's page can say how many rows match, opted in per toolset.

### Added

- **`SpecToolset(include_total=True)` adds `total` to every list tool's page.**
  Beside `items`, `page`, `totalPages` and `hasNext`, `total` is how many rows
  the selector matched across every page, so a model asked "how many" reads one
  number instead of paging through the rows or multiplying `totalPages`. It
  costs no query: the kernel's `paginate_output` already counts the rows before
  slicing them, and the flag hands that count on. With it on, the tool's
  `return_schema` lists `total` as required and the `limit` description names
  it. Off by default, and off the envelope, the schemas and the descriptions are
  unchanged. `SpecCapability` takes the same keyword. The flag is read by the
  default `shape_page`, so a project wanting it on some tools only overrides that
  seam, and the toolset page carries the recipe for declared aggregates (sums
  over the same filtered rows) as such an override.
- **django-spec-mcp 0.8.1 in `uv.lock`**, from 0.6.0. Lock only; no floor moved.

## [0.7.1] — 2026-11-04

A tool call reads a Validator as it declares itself to that call, on
django-service-specs 0.26.0, and the MCP bridge's wording helpers move into the
bridge's own module.

### Changed

- **`integrations/utils.py` is folded into `integrations/mcp.py`.** Its wording
  helpers served two MCP bridges until 0.7.0 removed one, so `MCPToolset` now
  carries them as module-private functions and the module is gone. The
  `non_field_key` parameter went with it, since the bridge always passed the
  kernel's `NON_FIELD_ERRORS`. Nothing public moved and no tool's wording
  changed.

### Fixed

- **A tool call reads a Validator as it declares itself to that call.**
  django-service-specs 0.26.0 lets a Validator declare
  `parameters(self, *, call_context=None)`, and `SpecToolset` now hands the
  call's context to the two kernel helpers a call runs before dispatch. A value
  `tool_server_arguments` binds is checked against what the Validator declares
  to the call: one the call's declaration refuses raises `ImproperlyConfigured`
  naming the tool, the server's error, where the model was handed a retry
  naming an argument it never sent. A seed-named field the tool's schema offers
  but the call's declaration drops is filled by the registered seed, the
  model's value dropped, where the call was refused with `Unknown argument`. A
  key a typed provider fills stays the server's when only the call's
  declaration names it: the model's value is dropped, where a malformed one was
  handed back as a retry. The schema and the checks run when the toolset is
  built still describe the spec to no call. The kernel floor is now
  `>=0.26.0`.

## [0.7.0] — 2026-11-04

The drf-mcp bridge, deprecated in 0.6.0, is removed, and a tool can pick its
own schema strategy.

### Added

- **A tool can pick its own schema strategy.** `ToolSpec.schema_strategy` and
  `@tool(schema_strategy=...)` take a `SchemaStrategy` that overrides the
  registry's for that tool's `ToolBinding.input_schema`, so one `ToolRegistry`
  can derive some tools by hand and others through pydantic. `None`, the
  default, keeps the registry's strategy, so no existing schema changes. An
  unknown strategy is refused with `ValueError` at registration.

### Removed

- **The drf-mcp bridge is gone, after its minor of deprecation.** The
  `[drf-mcp]` extra, `DRFMCPToolset` and its module
  `django_spec_agents.integrations.drf_mcp` are removed; bridge a django-spec-mcp
  server with `[mcp]` and `MCPToolset` instead, serving the same operations as
  kernel specs. `build_tool_catalog` no longer takes `drf_mcp_server=`: pass the
  server as `mcp_server=`, or the call raises `TypeError`. A failed call's
  message no longer carries a `step:` label, which only drf-mcp's chains sent.
  django-ag-ui before 0.74.0 and django-admin-agent before 0.59.0 still accept
  `drf_mcp_server=` and import `DRFMCPToolset` for it, which now fails at the
  first request; they set no cap on this package, so a host passing that
  keyword moves to `mcp_server=` with a django-spec-mcp server first.
- **`ToolFailurePolicy`'s default pass-through set no longer holds DRF's
  `PermissionDenied`.** It holds Django's `PermissionDenied` and, with the
  kernel, `NotPermitted` and `PrincipalUnavailable`. When a project's own
  tool raises `rest_framework.exceptions.PermissionDenied`, the policy now
  converts it into a failed call. To keep it ending the run, list it in
  `ToolFailureConfig(reraise=(...))` beside the three defaults.

## [0.6.2] — 2026-11-03

A permission refusal through either MCP bridge now ends the run.

### Changed

- **A permission refusal through either MCP bridge ends the run.** `MCPToolset`
  and the deprecated `DRFMCPToolset` raise Django's `PermissionDenied` for
  JSON-RPC `-32006`, where they raised `RuntimeError`. `ToolFailurePolicy`
  passes a `PermissionDenied` through by default, but it converted the
  `RuntimeError` into a failed call. The run then went on, and the model could
  keep trying ids. A refusal now ends the run, as a refusal from a built-in tool
  already did. Every other protocol fault still raises `RuntimeError`. A
  transport test that expected a refused call to be marked failed while the run
  finished now sees the run end.

## [0.6.1] — 2026-11-03

Fixes to the tool schemas and to `SpecToolset`'s build-time checks. Nothing
public is added or removed; one parameter shape can become required, below.

### Fixed

- **`pydantic` is declared, at `>=2.12`.** The schema derivation imports
  `pydantic` and `pydantic_core` itself, which arrived only through
  `pydantic-ai-slim`. The floor is the one `pydantic-ai-slim` 2.58 requires, so
  no install resolves differently.
- **`SpecToolset` refuses a `many` spec whose list is named after a pool seed.**
  A mount registering a seed named like the spec's `many_argument` (`items`
  unless the spec names another), or a spec naming its list `request` or `view`,
  was built and advertised the list as a required argument, then failed every
  call with `ImproperlyConfigured`. The toolset now raises that at construction,
  naming the tool and the list. Nothing that worked is refused.
- **A string reference nested in a tool's annotation resolves for a
  `functools.partial` and a callable instance**, as it already did for a plain
  or decorated function. `build_input_schema` read globals from the object it
  was given, which for either has none, while `inspect.signature` evaluates the
  top level in the partial's `func` or in `type(fn).__call__`. A module-level
  `_N = int` behind `ids: list[Annotated["_N", ...]]` gave `items: {}` under the
  hand-built strategy and raised `NameError` at registration under
  `SchemaStrategy.PYDANTIC`; it now resolves under both.
- **The hand-built schema leaves out an infinite or NaN bound or example.**
  `Field(le=float("inf"))` gave `"maximum": Infinity` and `ge=nan` or
  `examples=[nan]` gave `NaN`, which JSON does not have: `json.dumps(...,
  allow_nan=False)` and strict parsers refuse the schema. Such a bound is now
  left out, as a `Decimal` one is, and so are examples holding such a float.
- **The hand-built schema reads a `Field` given as a parameter's default**,
  `n: int = Field(ge=1, description=...)`, as it reads one inside `Annotated`:
  its description, title, examples and bounds are carried, and its default is
  shown. **A parameter can become required:** one whose `Field` has no default
  is now listed in `required`, as `SchemaStrategy.PYDANTIC` lists it, where it
  was optional with only its type. `ToolRegistry.call` binds the arguments as
  given, so an omitted one reached the tool as the `FieldInfo` itself.
- **`SchemaStrategy.PYDANTIC` keys a property by its parameter's name, not by
  a `Field` alias.** `s: Annotated[str, Field(alias="other")]` was advertised as
  `other`, which `ToolRegistry.call` refused with `TypeError`, since it binds
  the arguments by name. The schema is now `model_json_schema(by_alias=False)`,
  agreeing with the call and with the hand-built strategy; a nested model's
  aliased fields are keyed by name as well.
- **`ToolRegistry.call` and `acall` give an omitted `Field` parameter its
  default.** With `m: int = Field(5, le=9)` and `m` left out, the tool received
  the `FieldInfo` object, while the hand-built schema advertises `default: 5`, so
  a model trusting the schema omitted `m` and the tool broke. The call now binds
  the field's default, calling a `default_factory` once per call. A `Field` with
  no default is required in the schema, so omitting it raises `TypeError` naming
  the tool and the parameter, rather than passing the `FieldInfo`.

## [0.6.0] — 2026-11-03

Runs on django-service-specs 0.23.0 and adds the in-process django-spec-mcp
bridge, deprecating the drf-mcp one.

### Added

- **The `[mcp]` extra and `MCPToolset`**, in
  `django_spec_agents.integrations.mcp`: the in-process bridge over a
  django-spec-mcp server (`django-spec-mcp>=0.2.0`), serving the server's
  kernel-spec tools to an agent without a network hop. It behaves as the drf-mcp
  bridge does: the server's own `tools/list` schemas, per-step availability with
  the reason for each tool left out, `ModelRetry` for refused arguments, an
  unknown name, `input_required` and a `shapes` value the presenter refuses,
  `ToolFailed` with a refusal's `(code: ...)`, and `RuntimeError` for a protocol
  fault, such as a permission refusing the user (`-32006`). One answer differs:
  a service asking for an input its spec does not declare in `reads=` is a
  `RuntimeError` (`-32603`), which django-spec-mcp refuses where drf-mcp
  degraded it to `input_required`.
  A selector's `AdditionalInputRequired` also differs from `[specs]`: it arrives
  as a service error, so `ToolFailed` here and `ModelRetry` there.
  Its toolset `id` is `"mcp"`. The bridge and the extra import no Django REST
  Framework.
- **`build_tool_catalog(mcp_server=)`**, the server whose tools the catalog
  lists, taking a django-spec-mcp server or a drf-mcp one. Passing it with
  `drf_mcp_server=` raises `TypeError`.
- **`SchemaStrategy` and `schema_strategy=`**, on `build_input_schema` and on
  `ToolRegistry`, choosing how a tool's JSON Schema is derived.
  `SchemaStrategy.HAND_BUILT`, the default, is the package's own mapping;
  `SchemaStrategy.PYDANTIC` returns the `model_json_schema()` of a pydantic
  model over the same parameters, with pydantic's titles, `anyOf` for
  `X | None`, defaults and `$defs`, and raises where pydantic does. A value
  outside the enum raises `ValueError`. The schema the agent offers the model
  for an `@tool` tool is pydantic-ai's under either.

### Deprecated

- **The `[drf-mcp]` extra and `DRFMCPToolset`**: constructing the toolset warns
  `DeprecationWarning`, naming `MCPToolset` and `[mcp]`. Its behaviour is
  unchanged, bar the destructive-hint rule under Changed. Both are removed in
  the next minor.
- **`build_tool_catalog(drf_mcp_server=)`**, which warns `DeprecationWarning`
  and lists the server's tools as `mcp_server=` does. Removed in the next minor.

### Changed

- **The `[specs]` floor is django-service-specs 0.23.0**, the release
  django-spec-mcp 0.2.0 requires, so the lock and the lowest-versions job both
  resolve it; it was 0.16.0. `SpecToolset` advertises the kernel's
  `spec_caller_input_schema` in place of its own reading of `spec_input_schema`.
  Off a `many` spec the schema is unchanged. On a `many` spec each row is now
  read by what `input_data=` fills, as dispatch reads it: a row field it is
  annotated to fill leaves the row's `properties`, and one it may fill, or any
  field beside an untyped provider, is offered and not required. The rows used
  to require every field as declared.
- **A `DRFMCPToolset` tool registered `annotations={"destructiveHint": False}`
  is no longer gated.** The toolset marked a tool destructive on
  `readOnlyHint: False` alone, so `ToolGuard` kept gating a mutation its project
  had declared non-destructive; only a read-only override exempted it. A tool is
  now marked unless its `readOnlyHint` is true or its `destructiveHint` is
  false, which is how MCP reads the pair and how django-spec-mcp documents the
  override. A tool listed with no `annotations` is still not marked. A project
  that relied on the gate for such a mutation names it in `require_approval`.
  The new `MCPToolset` follows the same rule.
- **`ToolGuard` reads a tool's MCP annotations as the bridges do.** A tool
  whose metadata carries `annotations`, as `SpecToolset` writes them and as a
  remote MCP client stores a server's, was gated on `readOnlyHint: False` alone.
  A remote tool registered `annotations={"destructiveHint": False}` was gated
  where the in-process bridges leave it alone, and one carrying only
  `destructiveHint: True` was not gated where they mark it. The guard now gates a
  tool whose annotations say anything unless `readOnlyHint` is true or
  `destructiveHint` is false, a `None` field counting as unsaid; annotations
  that say nothing, `{}` or every field `None` (a server that sent an empty
  annotations object), still gate nothing. A project that relied on the gate
  for a remote `destructiveHint: False` mutation names it in
  `require_approval`. A remote tool whose annotations carry only `title`,
  `idempotentHint` or `openWorldHint` is now gated, as MCP's defaults read it;
  `exempt` un-gates it.

### Fixed

- **`build_input_schema` gave an `Annotated[T, ...]` parameter an empty schema.**
  `n: Annotated[int, Field(description=..., ge=1)]` produced `{"n": {}}`, so a
  schema built with it and attached through `toolsets=` imposed no type. It now
  reads `T`, including inside `list[...]` and `X | None`. An `@tool` tool the
  agent offers was never affected: pydantic-ai derives the model's schema from
  the function, `Field` included.
- **`build_input_schema` dropped a `Field`'s description and bounds.** Under the
  default strategy, `Annotated[int, Field(description="How many.", ge=1)]` now
  advertises `description` and `minimum: 1`. A `Field`'s `title` (only when one
  is passed) and `examples` are carried, and `ge`, `gt`, `le`, `lt`,
  `multiple_of`, `min_length`, `max_length` and `pattern`, from a `Field` or from
  bare `annotated_types` metadata, map to their JSON Schema keywords, a length
  to `minLength`, `minItems` or `minProperties` by what it bounds. Every value
  carried serialises to JSON: a bound that is not an `int` or `float` (a
  `Decimal`, a `date`) is left out, and `examples` are rendered in JSON mode.
  A schema without such metadata is unchanged.
- **`build_input_schema` gave a nested string reference an empty schema.**
  `Annotated["int", Field(ge=1)]`, `list[Annotated["int", "t"]]` and
  `list["int"]` produced `{}` where the reference stood; it is now resolved
  against the function's module globals, so each gives `{"type": "integer"}`
  (with `minimum: 1` for the first). A reference that does not resolve is still
  an empty fragment under the default strategy, and a `NameError` under
  `SchemaStrategy.PYDANTIC`.

## [0.5.0] — 2026-10-31

Requires Python 3.11 and pydantic-ai 2.58, as the rest of the family now does.

### Changed

- **Python 3.10 is dropped.** `requires-python` is `>=3.11`, the 3.10 classifier
  and its CI jobs are gone, and the lint target is Python 3.11.
- **`pydantic-ai-slim` is floored at 2.58**, in the core install and in the
  `[anthropic]`, `[openai]` and `[google]` extras, where the extras were at 2.16
  and 2.33. pydantic-ai 2.58.0 fixes GHSA-5pfm-2g3g-gh85, an advisory in its
  UI adapters (AG-UI and Vercel AI). This package imports neither (django-ag-ui
  serves AG-UI), so the floor does not by itself fix anything here: it lets the lock leave
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

[Unreleased]: https://github.com/Artui/django-spec-agents/compare/v0.13.0...HEAD
[0.13.0]: https://github.com/Artui/django-spec-agents/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/Artui/django-spec-agents/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/Artui/django-spec-agents/compare/v0.10.1...v0.11.0
[0.10.1]: https://github.com/Artui/django-spec-agents/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/Artui/django-spec-agents/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/Artui/django-spec-agents/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/Artui/django-spec-agents/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/Artui/django-spec-agents/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/Artui/django-spec-agents/compare/v0.6.2...v0.7.0
[0.6.2]: https://github.com/Artui/django-spec-agents/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/Artui/django-spec-agents/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/Artui/django-spec-agents/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/Artui/django-spec-agents/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/Artui/django-spec-agents/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/Artui/django-spec-agents/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Artui/django-spec-agents/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/Artui/django-spec-agents/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Artui/django-spec-agents/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Artui/django-spec-agents/compare/v0.0.0...v0.1.0
