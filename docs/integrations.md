# Integrations

Three optional extras, and one deprecated, all lazily imported — the base install stays slim and
none of these dependencies is pulled unless configured.

Two of them are **bridges**: `[specs]` and `[mcp]` expose specs you have
already declared as agent tools, so you don't re-declare your API as `@tool`
functions. The deprecated `[drf-mcp]` is the second bridge over a drf-mcp
server. Both make the agent act as the **logged-in user** — the request is
carried into every call, so your existing permission checks apply exactly as they
would over HTTP.

The third, `[harness]`, is a different shape: it brings in ready-made
**capabilities** (compaction, agent skills, step persistence and more) that plug
into the same `AgentConfig.capabilities` seam this package's own audit and
tool-guard capabilities use.

## Which bridge?

| | `[specs]` | `[mcp]` |
| --- | --- | --- |
| Path | `django-service-specs` spec → tool, in process | `django-spec-mcp` registry → toolset, in process |
| Needs an MCP server | **No** | Yes (but no network hop) |
| Use when | The agent is the consumer, and you have specs | You already run an MCP server and want the same tools in the agent |

If you have `ServiceSpec` / `SelectorSpec` objects and no MCP server, take
`[specs]`. If you already run an MCP server for external clients, take
`[mcp]` and get the identical tool surface without standing up a second one.

## `[specs]` — specs as tools

```bash
pip install django-spec-agents[specs]
```

`build_spec_capability(specs, request, exclude_names=…)` returns a
`SpecCapability` over the specs, bound to `request.user`. Each call runs through
the kernel's transport-neutral dispatch and enforces the spec's own
`permissions`.

Choosing the capability over a bare toolset is deliberate. The tool-set
conventions (list-tool `page` / `limit` / `ordering`, and the error contract)
live on `SpecToolset.get_instructions()`, so they reach the model either way and
the capability *delegates* rather than re-emitting them. What wrapping buys is
the capability seam itself —
`defer_loading`, and a uniform place to compose spec tools alongside audit and
guard.

A tool's input schema asks for what a call needs. A selector parameter with no
default, which the toolset does not fill, is listed in `required`, and a
single-item service advertises the lookup its target is resolved through, so a
tool changing one row names the `pk` that picks it. A call that leaves such an
argument out is a `ModelRetry` naming it, ``Missing required argument(s): `pk`.``,
so the model supplies it on the next turn rather than the run ending on the
selector's `TypeError`. The `[specs]` route does both, and
the `[mcp]` route asks for the same arguments and retries the same omission.
The extra is floored where a parameter a `kwargs=` provider
declines, and the model left out, is retried the same way rather than reaching
the selector, and where a selector parameter the toolset takes out of every
call, a list selector's `page` or `limit`,
makes `build_spec_capability` raise `ImproperlyConfigured` rather than
advertising an argument whose value never arrives.

### Declaring specs once

`specs` takes either a `name -> spec` mapping or a **spec registry** —
the kernel's `SpecRegistry`, the single declaration site for a project
exposing the same specs over more than one transport:

```python
from django_spec_agents.integrations.build_spec_capability import build_spec_capability

capability = build_spec_capability(registry, request)
```

The registry is matched **structurally**, through a `SpecSource` Protocol
declaring only `specs() -> dict`. The package root names no kernel type and
depends on `pydantic-ai-slim` alone; the kernel arrives only with the extra.

If you are writing a transport, normalise with `resolve_spec_mapping()` before
you touch the argument. Iterating a registry yields `RegisteredSpec` **records,
not names** — so a transport that reserves tool names by iterating the raw
argument fills its collision set with dataclasses and silently stops detecting
duplicates:

```python
from django_spec_agents.integrations.resolve_spec_mapping import resolve_spec_mapping

specs = resolve_spec_mapping(source)  # mapping or registry -> mapping
seen.update(specs)  # names, as intended
```

## `[mcp]` — an MCP registry as a toolset

```bash
pip install django-spec-agents[mcp]
```

`MCPToolset` bridges a [django-spec-mcp](https://github.com/Artui/django-spec-mcp)
server's registry through its public in-process surface (`list_tools` /
`acall_tool`), so the kernel's argument validation and the spec's permissions
apply as they would over HTTP — without the network hop and without reaching
into handler internals. The extra installs django-spec-mcp, which brings the
kernel; it brings no Django REST Framework, and the bridge imports none.

```python
from django_spec_agents.integrations.mcp import MCPToolset

toolset = MCPToolset(server, request, exclude_names=frozenset(registry_names))
```

Import it where the agent is built, never at a module a host import reaches:
django-spec-mcp needs a ready app registry to import.

Tool schemas come from the server's own `tools/list` rather than being
re-derived locally, so the bridge advertises the **same** `inputSchema` the
HTTP transport would — including a paged list tool's pagination arguments and
the `additionalProperties` policy.

That schema asks for what a call needs. A read the spec declares
`required=True` is listed in `required`, and a service tool also advertises the
target lookup its row is resolved through, so a tool changing one row names the
`pk` that picks it. `[specs]` builds its schemas the same way, so one spec asks
a model for the same arguments by either route. A paginated list selector
reading `page` is refused at registration, as `[specs]` refuses it
when the toolset is built, so one spec is served by both routes or refused by
both.

### A tool that cannot run now

A service's `affordances` can say when the operation is possible at all: a
condition written as a callable reads only the user and the request, never the
call's arguments, so while it is unmet every call is refused. django-spec-mcp
leaves such a tool out of `tools/list`, and whether one is unmet can change
mid-run. So the bridge lists definitions **once** per request, every tool the
user may see included, and on **every step**:

- offers only the tools the server would list right now, asking it through
  `MCPServer.aunavailable_tools`, so a tool that becomes
  available mid-run is offered from the next step without listing again;
- names each tool it left out in the toolset's instructions, with the reason
  its condition gives, so a model asked for one says why rather than guessing
  or denying the operation exists:

```text
- These operations exist but cannot be performed right now, so they are not among your tools. If the user asks for one, say it is unavailable at the moment and give the reason listed for it, rather than guessing why:
  - `close_books`: The books are closed.
```

That is word for word what `[specs]` says for the same spec, so the model
is taught one sentence whichever way a spec is exposed. A server declaring no
such condition adds nothing to the instructions and no thread hop per step.
Nothing here is an authorization decision: the call enforces every condition,
so a tool whose condition flips between the step offering it and the call is
refused with its code, as below.

### Error semantics

The bridge follows MCP's protocol-vs-tool boundary, which decides whether the
model gets to recover:

- a fault in the request the model produced → `ModelRetry`, so the model retries
  with the field errors fixed, or with a real name (the retry lists the tools
  this toolset advertises), instead of the run dying. django-spec-mcp answers
  arguments it refuses with a tool-level `validation_error` result, and an
  unknown tool name on JSON-RPC `-32602`; the bridge retries both. The retry is
  the server's message, then its field detail as JSON, unless the message
  already names every field in the detail, nested ones included, in which case
  the detail is left off rather than read twice. A detail is sent whole or not
  at all, so `Invalid arguments: {"a": ["Expected an integer."]}` keeps it,
  since that message names nothing. A reason under no field, which the server
  writes under the kernel's `NON_FIELD_ERRORS` key (a bare `DispatchError`, say),
  is kept unless the message quotes it. Refused arguments include a required
  read the call left out, which the model reads as
  ``Missing required argument(s): `pk`.``, word for word what `[specs]`
  raises for the same call. They also include a `shapes` value (a `fields`
  selection, say) the spec's presenter refuses while rendering, which the
  server answers as a `validation_error` naming the argument and quoting the
  presenter's reason; on a paged tool the likeliest one is a selection written
  against the page envelope rather than one item;
- a tool-level `input_required` result → `ModelRetry` naming the arguments to
  add on the next call. In process the server has no client to ask, so a
  service's `AdditionalInputRequired` for inputs its spec declares arrives this
  way;
- every other tool-level failure (`service_error`, `not_found`, a timeout, an
  oversized result) → `ToolFailed`, so the model reads the sentence and adapts,
  no retry budget is spent, and the call is recorded `outcome="failed"` for
  anything streaming the run;
- genuine protocol faults → a hard `RuntimeError` that aborts the run: nothing
  the model can rewrite its way out of. These are auth, a permission refusing
  the user (`-32006`), rate limits, and an internal error (`-32603`). The last
  is also the answer to a service asking, through `AdditionalInputRequired`,
  for an input its spec does not declare in `reads=`: no answer could reach the
  service, so declare every input a service may ask for.

A refusal keeps what names it. `ToolFailed` carries a single string, so when
the server serves an affordance refusal's `code`, it rides as a suffix on the
sentence:

```text
The books are closed. (code: books_closed)
```

That is word for word what `[specs]` produces for the same refusal raised in
process, so a spec reads the same to the model whichever way it is exposed,
and the code matches the one a row's `affordances` answer advertised. The
suffix is written for the model and for a person reading the tool call; a
program that needs the code reads it from the server's result, or on the
`[specs]` route off the exception through `translate_exception`, rather than
parsing it out of the sentence.

Bridged tools also carry destructiveness into the [tool guard](policy.md): the
bridge maps each tool's `readOnlyHint` annotation onto `DESTRUCTIVE_METADATA_KEY`,
so a service tool is destructive and a selector tool is not.

A service's own confirmation (`AdditionalInputRequired`) is the `input_required`
retry above, which the model can answer itself, on this route and on
`[specs]` alike. A selector's request does not agree: django-spec-mcp builds no
input round for a selector, so it arrives as a service error, and so as
`ToolFailed` here, where `[specs]` raises `ModelRetry`. It is not an approval gate: see
[what is gated, and what is not](policy.md#what-is-gated-and-what-is-not).

## `[drf-mcp]` — deprecated

```bash
pip install django-spec-agents[drf-mcp]
```

`DRFMCPToolset`, in `django_spec_agents.integrations.drf_mcp`, is the same
bridge over a `drf-mcp-server` registry, and behaves as above with drf-mcp's
names: its arguments are refused by the input serializer, a `QueryParam` the
output serializer refuses is the rendering retry, DRF's `NON_FIELD_ERRORS_KEY`
setting is the non-field key, and a chain tool's refusal also names the step
that failed, `(code: books_closed, step: void)`. It is deprecated, with the
extra: constructing one warns `DeprecationWarning`, and both are removed in the
next minor. Move to `[mcp]` and `MCPToolset`, serving the same operations as
kernel specs from a django-spec-mcp server. `build_tool_catalog`'s
`drf_mcp_server=` is deprecated with them: pass the server as `mcp_server=`.

## `[harness]` — upstream capabilities

```bash
pip install django-spec-agents[harness]
```

The other two extras bridge *your* API into the agent. This one is different: it
pulls in [`pydantic-ai-harness`](https://github.com/pydantic/pydantic-ai-harness),
a library of ready-made **capabilities** — the same seam this package's own audit
and tool-guard capabilities use. Nothing here wraps or re-implements them; you
construct one and hand it to `AgentConfig`:

```python
from pydantic_ai_harness.compaction import SlidingWindow

config = AgentConfig(model=..., capabilities=[SlidingWindow(max_messages=80)])
agent = build_agent(registry, config)
```

That is the whole integration. `capabilities` takes live instances (never dotted
paths), so a harness capability, a first-party one, and your own all compose the
same way, and pydantic-ai orders them by their own `get_ordering()`.

!!! warning "Version-sensitive"
    `pydantic-ai-harness` is 0.x and its minors may break, the `StepStore`
    protocol among them. Even so, the `[harness]`
    extra declares a **floor and no upper bound** (the number is in
    `pyproject.toml`). A one-minor ceiling over an upstream that releases this
    often made the package unresolvable on every harness release, whether or
    not anything broke. Three checks stand in for it instead:

    - **The lock** pins the harness and pydantic-ai every CI job runs against,
      and is moved to the newest deliberately rather than left to drift.
    - **The `lowest declared versions` job** runs the suite on every pull
      request against the oldest harness the extra allows, so the floor stays
      true.
    - **The weekly `upstream drift` run** ignores the lock, resolves the newest
      harness and pydantic-ai the extras allow, and runs the suite, so a
      breaking release is found by a scheduled job rather than by you.

    Recent harness releases pin `pydantic-ai-slim` to one exact release, so
    with `[harness]` installed the harness version you resolve also decides
    your pydantic-ai version.

### Long runs: compaction

A tool-heavy run grows its message history until it crowds the context window.
The `compaction` module trims it, and the choice is mostly about cost:

| Strategy | Cost | What it does |
| --- | --- | --- |
| `SlidingWindow(max_messages=80, keep_messages=40)` | free | Drops the oldest messages once a threshold is crossed, preserving tool-call / tool-return pairs. No model calls. |
| `ClearToolResults(max_messages=60, keep_pairs=3)` | free | Keeps the conversation shape but blanks old tool *results*, which are usually the bulk of the tokens. |
| `SummarizingCompaction(max_messages=60, keep_messages=20)` | **a model call** | Replaces the trimmed span with an LLM-written summary, so older context survives in compressed form. |
| `TieredCompaction(tiers=[...], target_tokens=…)` | varies | Applies cheaper strategies first, escalating only if still over target. |

Start with `SlidingWindow` — it is free and transparent. Reach for
`SummarizingCompaction` only when losing the old turns outright actually hurts,
since it spends a model call on every compaction.

Trimming happens inside `before_model_request` and is deliberately invisible to
the rest of the run: **nothing is emitted when a compaction fires**. A transport
that wants to tell the user "earlier turns were condensed" has to observe it
itself — `CompactionStrategy` is a one-method protocol
(`compact(messages, ctx) -> messages`), so a thin wrapper that compares its input
to its output is the seam for that.

### Progressive disclosure: agent skills

A *skill* is a folder with a `SKILL.md` — a name, a description, and a body of
instructions. `Skills` discovers them and exposes each as a **deferred**
capability: the model sees only the name and description up front, and the body
loads into context if it selects that skill. A dozen skills therefore cost a
dozen one-line descriptions, not a dozen instruction blocks.

```python
from pydantic_ai_harness.skills import Skills

config = AgentConfig(
    model=...,
    capabilities=[Skills("/srv/app/skills")],
)
```

`directories` takes one path or several. Discovery scans *immediate* child
directories for a `SKILL.md`, so `/srv/app/skills/summarise/SKILL.md` registers a
skill named `summarise` (the frontmatter `name` wins over the directory name when
both are present).

`include` / `exclude` select by exact skill name — and are **validated against
what was actually discovered**, so `exclude={"draft-only"}` raises `ValueError`
when no such skill exists rather than silently excluding nothing. That is the
behaviour you want (a typo'd name fails loudly), but it does mean a selection
list and a skills directory have to be kept in step.

!!! note "Instructions only, in v1"
    Upstream loads the `SKILL.md` body and nothing else — bundled scripts and
    resources are deferred to a future sandbox integration. A skill that assumes
    it can execute its own files will not work yet.

Each skill becomes one capability whose `id` is the skill name, which is what a
client needs to show "using skill X". Enumerate them with `skills.apply(visitor)`.

### Also available

`step_persistence` (used by [`DefaultStepStore`](storage.md)), `subagents`,
`code_mode`, `overflowing_tool_output`, `guardrails`, `filesystem`, `shell` and
`ManagedPrompt` all compose the same way. None of them need support from this
package — if it is a pydantic-ai capability, `capabilities=[...]` takes it.

## Name collisions

Both bridges take `exclude_names`, and precedence is fixed: the `@tool` registry
wins, then the MCP server's, then spec tools. A duplicate that slipped through would make
pydantic-ai raise `UserError` at run time, so the excluded set is threaded
through composition instead.

Full signatures in the [integrations reference](reference/integrations.md).
