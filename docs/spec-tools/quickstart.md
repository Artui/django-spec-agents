# Quickstart

## 1. Have some specs

`SpecToolset` works with the `ServiceSpec` and `SelectorSpec` objects you
declare on [django-service-specs](https://github.com/Artui/django-service-specs),
the kernel, with no DRF in them (see
[a spec declared on the kernel](spec-registry.md#a-spec-declared-on-the-kernel)).
A read selector and a write service, their input and output declared as
dataclasses:

```python
from dataclasses import dataclass

from django_service_specs import (
    DataclassPresenter,
    DataclassValidator,
    SelectorKind,
    SelectorSpec,
    ServiceSpec,
    Unrestricted,
)


@dataclass
class OrderOut:
    id: int
    status: str


@dataclass
class OrderIn:
    status: str


def list_orders(*, user):
    """List the current user's orders."""
    return Order.objects.filter(owner=user)


list_orders_spec = SelectorSpec(
    kind=SelectorKind.LIST,
    selector=list_orders,
    presenter=DataclassPresenter(OrderOut),
    permissions=[Unrestricted()],
)


def create_order(*, data: OrderIn, user):
    """Create an order for the current user."""
    return Order.objects.create(owner=user, status=data.status)


create_order_spec = ServiceSpec(
    service=create_order,
    validator=DataclassValidator(OrderIn),
    presenter=DataclassPresenter(OrderOut),
    permissions=[Unrestricted()],
)
```

## 2. Build the toolset

```python
from django_spec_agents.spec_tools import SpecToolset

toolset = SpecToolset(
    {
        "list_orders": list_orders_spec,
        "create_order": create_order_spec,
    }
)
```

Each key is the tool name. The description comes from the selector/service
docstring, the parameter schema from the selector's reads or the service's
Validator and target lookup (see
[What a tool asks the model for](#what-a-tool-asks-the-model-for)), the
`return_schema` from its presenter's output, and the `readOnlyHint` annotation
from the spec kind (selectors read, services mutate). List selectors
additionally accept `page` and `limit` tool args, plus `ordering` where the
selector's [`filter_set` declares one](#ordering).

## 3. Run an agent

The acting user flows through `RunContext.deps`. The default
[`AgentDeps`](../reference/agent.md#django_spec_agents.AgentDeps) carries it:

```python
from pydantic_ai import Agent
from django_spec_agents import AgentDeps

agent = Agent("anthropic:claude-opus-4-8", deps_type=AgentDeps, toolsets=[toolset])

result = await agent.run(
    "show me my last 5 orders, newest first",
    deps=AgentDeps(user=request.user),
)
```

!!! tip "No request in sight?"

    `request.user` is the HTTP shape. A Celery task, a management command or a
    scheduled job resolves the acting user itself and passes it the same way —
    see [Running from a worker](background-runs.md), which also covers the one
    setting a fan-out genuinely needs.

For that request the model can call `list_orders` with
`{"limit": 5, "ordering": ["-created"]}` and the toolset enforces permissions,
runs the selector as `request.user`, hands `ordering` to the selector's
[`filter_set`](#ordering), slices the result, and renders it through
its presenter.

## Every list result is a page

A list selector answers with the pagination envelope, never a bare array:

```python
{
    "items": [{"id": 12, "total": "48.00"}, ...],
    "page": 1,
    "totalPages": 4,
    "hasNext": True,
}
```

`limit` defaults to 100 rows and `page` to 1, so a tool over a large table
returns its first hundred rows **and says so**. That is the point: a bare slice
would leave a model asking for a collection with 50 of 51 rows and nothing in
the answer telling it more existed. `hasNext` closes that gap — the model can
ask for `page: 2`, or narrow the request with a filter, instead of answering
from a page it took for the whole set.

Because the toolset takes `page` and `limit` out of a list tool's call to serve
the page, a list selector taking a parameter of either name is refused with
`ImproperlyConfigured` when the `SpecToolset` is built: the parameter would never
receive what the model sent, and one with a default would quietly run on it.
Rename the parameter. A retrieve selector's `page` is not paginated and reaches
it.

`max_page_size` caps `limit` and advertises itself as JSON-Schema `maximum`
on it. A call naming no `limit` is served the smaller of 100 and the ceiling, and
that is the default both the `limit` description and the toolset's instructions
state, so with the toolset below the model reads "Defaults to 25" beside
`maximum: 25`. A ceiling above 100 raises what a call may ask for, not what it
gets by asking for nothing: the stated default stays 100.

```python
toolset = SpecToolset(specs, max_page_size=25)
```

The same envelope is what the tool's `return_schema` describes, generated from
the same spec — so the schema and the payload cannot disagree about it.

## The output schema

Each tool definition carries a `return_schema` derived from the spec's presenter
output, projected the same way the payload is: a field marked
[hidden](agent-audience.md) is absent from both, and a marked handle carries its
description in both. It is the kernel's `spec_output_schema`.

A spec's `metadata["json_schema"]` is merged over both schemas, keyed by phase:
`{"input": {...}}` over the
parameter schema and `{"output": {...}}` over the `return_schema`. The merge is
shallow, so a key the fragment names is the fragment's whole, and an `output`
fragment never makes a `return_schema` for a spec with none. A fragment that is
not keyed by phase is refused when the toolset is built, naming the tool.

It is populated but **not sent** by default, because a return schema costs
context on every turn of every run and only your model and your outputs say
whether that trade is worth it. Pydantic-AI owns the opt-in, at either scope:

```python
agent = Agent(model, toolsets=[SpecToolset(specs).include_return_schemas()])
```

A spec with no presenter gets `None` rather than a guessed shape.

A spec declaring [`shapes=`](#read-shaping-parameters) requires no field of a
row, at any depth, where the kernel's schema requires each one its presenter
always renders. A shape reaches the presenter alone, and a `fields` selection
there leaves keys out, so no key is present under every value the model may
send. The `affordances` each row carries stays required, since dispatch writes
it whatever the shapes, as do the four keys of a list tool's page.

A `return_schema` admits `null`, with the root `type` `["object", "null"]`,
where the spec says dispatch may hand the model `None`, as the kernel's
`can_present_nothing` answers it for every route that states an output schema:

- a retrieve selector declaring `allow_none=True`, when nothing matches;
- a service whose `output_selector_spec` re-reads through a `selector`, because
  dispatch materializes the re-read with `.first()`, and a re-read that filters
  out the row it wrote finds none;
- a service with nothing to re-read that declares `ServiceSpec(allow_none=True)`,
  since dispatch then presents the service's own return, a `None` included, a
  `many=True` service's too, whose `return_schema` is then `["array", "null"]`.

**A `None` the spec does not declare is still handed to the model.** A service
with nothing to re-read and no `allow_none=True` that returns `None` is
presented as `None`, against a `return_schema` whose root is `"object"` and does
not admit `null`. `ServiceSpec(allow_none=True)` is the declaration that admits
it, so declare it on any such service that can return nothing.

`allow_none` on a service's nested `output_selector_spec` is not read, because
dispatch does not read it, and a list result is never `null`, only empty. A
service whose `output_selector_spec` declares `LIST` is an array whether or not
it has a `selector` to re-read through: with none, dispatch presents the
service's own return as the list, and a return that is not a set of rows, such
as the one row a create returns, raises `ImproperlyConfigured` out of the run.
Return the rows, or declare `RETRIEVE` to present one.

## What a tool asks the model for

A tool's parameter schema is the model's only account of what to send, and the
kernel reflects nothing off a callable's signature: every argument is one the
spec declares. So the schema is the kernel's `spec_input_schema` of the
compiled spec, the argument set `dispatch` checks, less the kernel's
`server_owned_keys` for the toolset's seeds:

- **A name the toolset fills is not advertised.** That is the dispatcher's own
  pool seeds (`request`, `user`, `progress` and the rest), every name registered
  in [`pool_seeds`](#project-pool-seeds), and the keys a `kwargs=` provider
  returns when its return annotation is a `TypedDict`, less any it may decline
  with `UNSET` (below). The annotation is read by the kernel's
  `provider_keys`, the reader dispatch's own contract is stated in, so this
  toolset and drf-mcp read a provider the same way.
- **An input field the spec's Validator requires and no call can send** is
  refused when the toolset is built, with `ImproperlyConfigured` naming the
  tool and the field, under either [`unknown_arguments`](#unexpected-arguments)
  policy. A Validator says so as its `unsendable` attribute, a collection of
  field names: a field it fills itself under a name dispatch seeds, so it
  declares no parameter for it, the tool does not advertise it, and the model's
  value would be refused or dropped. Rename the field or make it optional. A
  Validator that does not answer `unsendable` names none, and a spec compiled
  `partial` is left alone. A field named like one of your own
  [`pool_seeds`](#project-pool-seeds), such as `tenant`, is a parameter, so it
  is the model's to send and is not refused.
- **A read declared `required=True` is required**, and one with a `default` or
  none stays optional.
- **A `kwargs=` provider without a `TypedDict` return annotation may fill any
  read**, a plain `dict` or a lambda included, so no read its values reach is
  required of the model, one declared `required=True` included: the tool
  advertises each and the kernel checks it on the pool the call assembles
  instead, handing the model back a read neither of them filled. A provider
  that only reads, and fills nothing, says so with an empty `TypedDict`, and
  the reads beside it keep their `required`. So may a provider whose return annotation does not resolve. Only the
  return annotation is read for the keys, so a parameter typed with a name
  imported only under `TYPE_CHECKING` changes nothing, and a `TypedDict` value
  that does not resolve makes only its own key one the provider may decline, so
  that key stays advertised and the others are still filled. Annotate the
  provider to have the rest required:

```python
from typing import TypedDict


class ProjectScope(TypedDict):
    ceiling: int


def scope(project_pk) -> ProjectScope:
    # ceiling is filled here, so the model is never asked for it. project_pk is
    # a read the tool declares, handed to the provider by name.
    return {"ceiling": ceiling_for(project_pk)}


list_spec = SelectorSpec(
    kind=SelectorKind.LIST,
    selector=priced_under,
    reads=Parameters.of(
        Parameter("project_pk", "integer", required=True),
        Parameter("ceiling", "integer", required=True),
    ),
    kwargs=scope,
    permissions=[Unrestricted()],
)
```

- **A key whose value admits `UnsetType` is not one the toolset fills.** A
  provider may decline a key by returning the kernel's `UNSET` for it, which
  drops the key from the pool and lets the model's value through, so
  `ceiling: int | UnsetType` keeps `ceiling` advertised for the model to send,
  and optional, since the provider may fill it instead. The provider's other
  keys are still not asked for. It is the key's own value that has to admit
  `UNSET`, as a union member: `regions: list[str | UnsetType]` always comes
  back as a list, so `regions` is filled. A generic `TypedDict` is read with
  what binds its type parameters, so `-> Scope[str | UnsetType]` declines
  `Scope`'s `tenant: T` as the written-out `tenant: str | UnsetType` does.
  If the provider declines the key and the model has not sent it either,
  the kernel refuses the call before the selector runs, and the model is
  handed back ``ceiling: This field is required.`` to send it on its next
  turn, as it is when an untyped provider leaves a parameter unfilled.
  Where the selector can run without the value, give the read a default.
- **A name only a
  [`call_seeds`](reference.md#django_spec_agents.spec_tools.SpecToolset.call_seeds)
  override fills is not a read.** An override is code the schema cannot read,
  so leave the name out of `reads=`: the callable takes it by name from the
  pool, as it takes `user`. Declared as a read, it is the model's to send, and
  a seed of that name is refused at the call with `ImproperlyConfigured`: the
  seed would replace whatever the model sent, so a model that asks for one
  project would be served another's rows. Where the value is the same for
  every run, register it as a [pool seed](#project-pool-seeds) instead, which
  the schema does read.

**A service tool also advertises its target lookup**: the reads of its
`collection_selector_spec` or its `instance_selector_spec`, whichever the
service declares, beside the Validator's fields. A rename tool whose instance
selector reads `pk` asks for `pk` as well as the new title, and requires it. A
lookup parameter and a Validator field sharing a name are two declarations of
one argument, and the toolset refuses the spec when it is built with
`ImproperlyConfigured` naming the tool and the parameter, rather than
advertising one of them. Rename one of the two.

**A call that leaves out a required argument is handed back**, the tool's own
or its target lookup's, as `ModelRetry` naming each one left out, as in
``Missing required argument(s): `pk`.``, so the model corrects the call on its
next turn. A required Validator field is checked by the Validator once the call
runs, so a service call missing both `pk` and a field is told about `pk` first,
and about the field on the turn after. A name a provider may fill is not
checked before the call, since only the pool the call assembles can say
whether it arrived, and the kernel checks that pool: a read nothing filled
comes back as a retry naming it too, ``ceiling: This field is required.``

## Custom identity

If your project carries identity on a richer deps object, hand the toolset a
`get_user` extractor instead of using `AgentDeps`:

```python
toolset = SpecToolset(specs, get_user=lambda ctx: ctx.deps.principal.user)
```

## Project pool seeds

Over HTTP a service reads its tenant, locale or clock off `request`. Off HTTP
there is no request carrying any of that, and the kernel's
[`PoolSeeds`][django_service_specs.PoolSeeds] is the channel
for it. Hand the registry to the toolset as `pool_seeds=`:

```python
from django_service_specs import DEFAULT_POOL_SEEDS

seeds = DEFAULT_POOL_SEEDS.extend(tenant=lambda user: user.profile.tenant)
toolset = SpecToolset(specs, pool_seeds=seeds)
```

Every dispatch the toolset makes is handed the registry. A service, a selector
or an affordance's condition that declares `tenant` therefore receives the
resolver's value, as it would from the kernel's `dispatch(pool_seeds=seeds)`
called directly. The per-step check that
[leaves out an unavailable operation](#an-operation-that-is-unavailable-right-now-is-left-out)
asks its conditions against the same seeds, so a step's catalog and the call's
own refusal answer alike.

A registered name is reserved the way `user` and `request` are:

- **The model cannot supply it.** An argument of that name is not passed on and
  is not refused as unknown either, so the callable always sees the resolver's
  value.
- **A read cannot declare it.** A read with that name in a spec's `reads=`
  raises `ImproperlyConfigured` when the toolset is built, as one named `user`
  does. The kernel refuses an argument that meets a seed, so an accepted read
  would offer the model an argument every call is then refused for.
- **No schema advertises it.** A selector or target lookup declaring the seed
  as a parameter does not offer it to the model, so the model is not asked for
  a value the call would ignore. See
  [What a tool asks the model for](#what-a-tool-asks-the-model-for).

The registry applies to the whole toolset. A seed is ambient to the
deployment, and what varies with the acting user belongs in the resolver,
which declares `user` to receive it. What varies with the run or the tool
being called is the [per-call form](#per-call-seeds-and-the-runs-context).

## Per-call seeds and the run's context

Two things reach every call beside its arguments, and neither is a request
the toolset builds.

**The run's context** is `AgentDeps.call_context`, a plain `dict` the code
starting the run fills. Each call is handed its own copy, with the tool's name
under `action`, and `request`, `view` and `format` set to `None` where the run
put none. The run's entries reach every call as the same objects, so put the
request the run arrived on there, under `request`:

```python
deps = AgentDeps(user=request.user)
deps.call_context["request"] = request
result = await agent.run(prompt, deps=deps)
```

A callable declaring `request` is then handed that request, and a permission
check declaring `call_context` reads it there. The toolset builds no request
and no view of its own, so a selector or provider declaring `request` or `view`
receives `None` unless the run puts one here or a
[`call_seeds`](#per-call-seeds-and-the-runs-context) override returns one. A
value it would read off a view's captures or a request's query string it takes
by name instead, as one of the spec's reads. What a call writes to its
copy does not reach the next call, nor the run's dict, so two calls the model
makes at once never see each other's `action`.

**Per-call seeds** come from
[`call_seeds`](reference.md#django_spec_agents.spec_tools.SpecToolset.call_seeds),
asked once per call with the live `RunContext` and the tool's name, and once
per conditioned tool when the catalog is listed. Each value is a resolver, as
for `PoolSeeds.extend`:

```python
class TenantToolset(SpecToolset):
    def call_seeds(self, *, ctx, tool_name):
        tenant = ctx.deps.tenant
        return {"tenant": lambda: tenant}
```

A name the mount's `pool_seeds` registers already is refused with
`ValueError` at the call, and `request` or `view` returned here replace the
call's own.

## What a permission check sees

Every call is authorized by the spec's `permissions`, the kernel's
`PermissionCheck`s, inside the kernel's dispatch: `has_permission` before
anything is called and `has_object_permission` on the resolved row. Each is
asked with the call's **principal**, the acting identity: `deps.user`, or
whatever `get_user` returns. The request a run arrived on never contributes an
identity.

A check reading more of the call than its principal names it, as the kernel's
`PermissionCheck` documents: a keyword `arguments` is handed the call's
arguments, as a read-only mapping, a keyword `call_context` is handed the
[run's context](#per-call-seeds-and-the-runs-context), with the tool's name
under `action`, and a keyword named after a registered
[pool seed](#project-pool-seeds) is handed that seed's value.

### The tool catalog is not permission-filtered

`get_tools` advertises every spec to every run, except a tool whose [operation
condition is unmet](#an-operation-that-is-unavailable-right-now-is-left-out) at
that step. A tool whose permissions will deny this caller is still listed — the
denial happens on the call. That is
deliberate: a permission whose answer depends on the arguments has none to read
at listing time and would hide a tool the caller can actually use, the listing
runs once per model step so a database-backed check would cost a query per spec
per step, and a model that cannot see a tool cannot ask about it. A listing
carries a name, a description and an input schema; no row data.

If a deployment does want a narrower catalog, override `is_tool_listed`:

```python
from asgiref.sync import sync_to_async


class OpsOnlyToolset(SpecToolset):
    async def is_tool_listed(self, name, ctx):
        if name != "suspend_account":
            return True
        # Django refuses ORM access on the event loop, so anything that
        # queries has to go through a thread — as dispatch itself does.
        return await sync_to_async(ctx.deps.user.groups.filter(name="ops").exists)()
```

Hiding a tool is a disclosure decision, never an authorization one: the call is
gated by the spec's `permissions` whatever this returns.

### An operation that is unavailable right now is left out

A spec's `Affordance` answered without a row, a callable `when` such as
`lambda: Period.current().is_open` (see [below](#a-refused-affordance-names-its-code)),
is asked on every model step, and a tool whose condition is unmet is left out of
that step's catalog. The arguments against filtering by permission do not apply
to it. The condition reads only the seeds (the user, the request and any
[registered seed](#project-pool-seeds)), never an argument, so it cannot hide a tool the caller could
have invoked with other arguments. Only the specs declaring one are asked, and a
toolset declaring none pays nothing. And the model can still tell the user about
the missing tool, because the instructions for that step end by naming each tool
left out with its reason:

```text
- These operations exist but cannot be performed right now, so they are not among your tools. If the user asks for one, say it is unavailable at the moment and give the reason listed for it, rather than guessing why:
  - `post_invoice`: The books are closed.
```

The rest of the instructions are derived from the tools that step offers, so no
line describes a tool it lacks. An `instructions=` override replaces those
conventions but not this list, which is appended after the override: which
tools a given step lacks is not something an override written in advance can
say. The heading is the `unavailable_heading` field of
[`conventions=`](#changing-what-the-model-is-told), and setting it to `None`
drops the heading and the list, with an override or without one.

A few things follow from how it is asked:

- **An `is_tool_listed` override cannot put the tool back.** The omission is
  applied beside it rather than inside the default, so an override returning
  `True` does not need to call `super()` to keep it.
- **Conditions run where a dispatch runs**, in a thread under the toolset's
  `thread_sensitive` and `executor`, so one that queries is fine. The connection
  it opens is closed after it, as a dispatch's is.
- **A condition is asked with the seeds its call would register**, so one
  reading `request` sees the request the run arrived on when the catalog is
  listed as at the call, `None` for a run that arrived on none, and one reading
  a name a [`call_seeds`](#per-call-seeds-and-the-runs-context) override
  returns is asked with the seed returned for its own tool. That listing call
  carries no arguments, and its context's `action` is the tool's name.
- **A condition on the row (an ORM expression) is never asked here.** There is
  no row when the catalog is listed, so the call answers it for its own row.
- **`get_tools` and `get_instructions` each ask once per step.** A shared answer
  would need a key scoping it to one run, and the run context has none that is
  safe: `run_id` can be supplied by the caller and is reused when a failed run
  is retried. A condition that flips between the two reads can leave one step's
  catalog and instructions disagreeing about one tool.

None of this changes the call. The kernel's `dispatch` enforces every affordance
whatever was listed, a call against a listing that went stale is refused as
[below](#a-refused-affordance-names-its-code), and a model calling a tool that
was left out gets pydantic-ai's unknown-tool retry, which names the tools that do
exist.

## Changing what the model is told

Every line of the instructions block, the heading of the unavailable list
included, is a field of
[`AgentConventions`](reference.md#django_spec_agents.spec_tools.AgentConventions),
and so are three sentences outside it: the description a handle field gets when
it declares none, the sentence scoping a paged tool's read-shaping parameters,
and the retry for a missing argument. `conventions=` changes them one line at a
time. Each field defaults to the toolset's own wording, so `AgentConventions()`
changes nothing, and leaving `conventions=` unset is the same as passing it.

Nothing else the toolset writes is a field. The descriptions of the arguments it
adds, such as `limit` and `page`, stay fixed, and so do its other retries: a
`limit` or `page` that is not a positive integer, an `ordering` the tool does not
take, a service asking for more input, and the opening of a
[render retry](#a-selection-the-presenter-rejects), whose closing scope
sentence is the one part that is a field.

```python
from django_spec_agents.spec_tools import AgentConventions, SpecToolset

toolset = SpecToolset(
    specs,
    conventions=AgentConventions(
        # {page_size} is the page an omitted `limit` is served; a literal brace is doubled.
        pagination=(
            "- Collections come back one page at a time, as "
            '{{"items": [...], "page": 1, "totalPages": N, "hasNext": true|false}}, '
            "{page_size} items unless you pass `limit`. Ask for the next `page` while "
            "`hasNext` is true."
        ),
        handles=None,  # this audience never sees identifiers, so say nothing about them
    ),
)
```

**A field changes what a line says, never whether it appears.** The toolset still
decides that: the `pagination` line above is still absent from
a toolset with no list tool, and the block is still re-derived on each step an
[unmet condition](#an-operation-that-is-unavailable-right-now-is-left-out) leaves
a tool out, so an overridden line goes with the tool it describes. `None` drops a
line wherever it would have been said.

| Field | Where it lands | Said when | Placeholders |
| --- | --- | --- | --- |
| `base` | instructions, first | always | none |
| `pagination` | instructions | some tool returns a page | `{page_size}` |
| `ordering` | instructions | some tool advertises a sort argument | `{names}` |
| `handles` | instructions | some tool's output marks a handle | none |
| `read_shaping` | instructions | some tool declares a read-shaping parameter | `{names}` |
| `read_shaping_on_pages` | instructions, after `read_shaping` | that tool also returns a page | none |
| `unavailable_heading` | instructions, last, above the tools left out | a condition left a tool out this step | none |
| `handle_field_description` | a handle field's output schema | the field declares no description of its own | none |
| `query_param_on_pages` | a read-shaping parameter's description, and the [render retry](#a-selection-the-presenter-rejects) | the tool returns a page | none |
| `missing_arguments` | the retry for a call missing an argument | a required read or lookup parameter is left out | `{names}` |

A few rules follow from what each field is:

- **Every field is a `str.format` template**, placeholders or not, so a literal
  brace is written twice everywhere. `{names}` is each name in backticks, joined
  with `, ` (sorted, except `ordering`'s, which follow the tools).
- **A typo fails at startup.** A placeholder the field does not accept, an
  unbalanced brace, or a format spec its value cannot take raises
  `ImproperlyConfigured` naming the field when the `AgentConventions` is built.
- **`None` on `read_shaping` drops `read_shaping_on_pages` too**, because that
  sentence continues it; `None` on `unavailable_heading` drops the list beneath
  it. To drop a line, use `None`, not `""`, which leaves a blank line.
  `missing_arguments` cannot be `None`, empty or only whitespace: it is the
  retry's whole text.
- **`missing_arguments` covers the check made before the spec's Validator
  runs.** A field the Validator requires is still reported in the Validator's
  own words, and a parameter a provider may fill and did not in the kernel's
  (``ceiling: This field is required.``), since only the pool it assembles can
  say the value never arrived.

**Some lines state facts about behaviour, and an override owns keeping them
true.** `pagination` and `query_param_on_pages` name the page envelope's keys, and
`base` says which failures come back as a retry and which as a failed call. The
toolset goes on behaving as the defaults describe whatever the text says, so a
rewording that drops or contradicts one of those facts tells the model something
false.

**`conventions=` and `instructions=`.** `instructions=` replaces the whole
derived block, so changing one of its lines (`base` through
`read_shaping_on_pages`) beside it would be ignored, and the toolset refuses the
pair with `ImproperlyConfigured` naming the fields. The other four land outside
the block and apply either way, `unavailable_heading` included, since the list of
unavailable operations is appended after an override too. Prefer `conventions=`
where it can say what you need: an override is a copy of the block, so it keeps
advising about pagination on a step with no list tool, and it misses every
correction a later release makes to the wording.

`SpecCapability` takes the same keyword, and
[`SpecCapability.from_toolset`](reference.md#django_spec_agents.spec_tools.SpecCapability.from_toolset)
keeps the conventions of the toolset it wraps. They reach this toolset's tools
only: the MCP transport, djangorestframework-mcp-server, writes its own schemas
and retries, so a model reading the same specs over MCP is told what that
transport is configured to say.

## Unexpected arguments

By default the toolset **rejects** tool args outside a spec's declared input set
— a key the model invented — surfacing them as a `ModelRetry` so the model
self-corrects. A `filter_set`'s filters are declared names, so a key outside them
and the selector's parameters is refused like any other. A selector taking a bare
`**kwargs` declares no name, so every key sent to it is refused. Pass
`unknown_arguments=` to change this:

```python
from django_service_specs import UnknownArguments

# silently drop unexpected keys instead of rejecting them
toolset = SpecToolset(specs, unknown_arguments=UnknownArguments.IGNORE)
```

`IGNORE` drops a key no parameter declares. The tool's schema says which:
under `REJECT` it closes the argument set with `additionalProperties: false`, at
the top and on each row of a [list input](#a-list-as-input), and under `IGNORE`
it states nothing, since a key outside the set is dropped and the call served.
Under either policy the key never
reaches the callable: the kernel hands a callable only what its spec declares.
One exception is known: a selector taking `**kwargs` beside a `filter_set` also
receives the FilterSet's values in its catch-all.
`REJECT` and `IGNORE` are the two the toolset accepts. `PASSTHROUGH` raises
`ImproperlyConfigured` when the toolset is built, because a tool call is one flat
object and has no other channel to carry an undeclared name to the callable; to
hand one over, declare it, as a `reads` entry or a Validator field.

## A list as input

A `ServiceSpec` declaring `many=True` becomes a tool that takes its list as **one
named argument**. Its input validates as a JSON array, and a model's tool
arguments are always a JSON object, so the array travels under the argument the
spec's `many_argument` names, `items` unless it names another:

```python
@dataclass
class WidgetIn:
    name: str
    price: str


def create_widgets(*, data: list[WidgetIn], user):
    """Create several widgets in one call."""
    return [Widget.objects.create(owner=user, name=w.name, price=w.price) for w in data]


create_widgets_spec = ServiceSpec(
    service=create_widgets,
    validator=DataclassValidator(WidgetIn),
    presenter=DataclassPresenter(WidgetOut),
    many=True,
    many_argument="widgets",  # optional; the default is "items"
    permissions=[Unrestricted()],
)
```

The model sends `{"widgets": [{...}, {...}]}` and the service receives the list,
exactly as it would from a bare array body. The tool's parameter schema says so:
an object with that one required property, an array of the item schema,
carrying the spec's `many_min_length` and `many_max_length` as `minItems` /
`maxItems`, and, under the default [`REJECT`](#unexpected-arguments),
`additionalProperties: false`. A read the spec declares is advertised beside
it.

What the model is told when a call is wrong:

- **An invalid item** comes back as a `ModelRetry` naming the argument, then the
  invalid item's index, then the field, as `widgets[1].price: ...`, the same
  rendering every validation error gets, so the model corrects that one item.
- **Any other argument sent beside the list** is held to the toolset's
  `unknown_arguments`, as any undeclared key is: under `REJECT` it comes back
  as `note: Unknown argument.`, and under `IGNORE` it is dropped, since the
  service receives only the list and the argument would have nowhere to go.

A tool that needs arguments **beside** its list cannot be `many=True`. Name the
list as a field of the Validator's input instead, and loop over it:

```python
@dataclass
class BulkWidgetIn:
    items: list[WidgetIn]
    dry_run: bool = False
```

The model sends `{"items": [...], "dry_run": true}`, and an invalid item comes
back placing its errors at its index, as `items[1].price: ...`.

## Ordering

**The `filter_set` owns ordering.** Declare a django-filter `OrderingFilter`
named `ordering` on the selector's FilterSet and you are done:

```python
import django_filters


class OrderFilterSet(django_filters.FilterSet):
    ordering = django_filters.OrderingFilter(
        fields=(("created_at", "created"), ("total_cents", "total")),
    )

    class Meta:
        model = Order
        fields = ["status"]
```

The kernel lists that filter in the tool's input schema as an array of its
public choices, `{"type": "array", "items": {"type": "string", "enum":
["created", "-created", "total", "-total"]}}`, as `OrderingFilter` takes a list
of sort keys, so the model is told exactly what it may sort by, in the words the
FilterSet uses. The choices' labels are not carried. A single key is sent as a
one-element list, `["-created"]`; one sent as a bare string is handed back as
`ordering: Expected an array.` for the model to correct, rather than read as
one key. At call time the value is handed to
the FilterSet as filter data: it validates the choice, applies its own
`param_map`, and a value outside the enum comes back as a `ModelRetry`. The
toolset contributes nothing and takes nothing away.

One vocabulary, one declaration site, and the same ordering your HTTP views
already serve.

The filter's name is yours to pick — an `OrderingFilter` declared as `sorting`
is found and used the same way. What the schema advertises is what the model may
send, under whatever it is called.

### A list selector with no `filter_set`

A selector that takes its own sort argument works too: declare it as a read,
and it is advertised like any other, and handed to the callable to apply.

```python
def list_orders(*, user, ordering: str):
    """List the acting user's orders."""
    return Order.objects.filter(customer=user).order_by(ordering)


list_orders_spec = SelectorSpec(
    kind=SelectorKind.LIST,
    selector=list_orders,
    reads=Parameters.of(Parameter("ordering", "string", default="-created_at")),
    presenter=DataclassPresenter(OrderOut),
    permissions=[Unrestricted()],
)
```

Prefer the `FilterSet` where there is one: it validates the value against a
published set of choices before anything reaches the ORM, while a bare parameter
is only as safe as what the selector does with it.

### Migrating from `ordering_fields`

`SpecToolset(specs, ordering_fields=[...])` and its per-tool
`tool_ordering_fields` form do not exist; passing either raises `TypeError` at
construction. Move the names onto an
`OrderingFilter` as `(orm_path, public_name)` pairs — the FilterSet at the top of
this section is exactly `ordering_fields=["created_at", "total_cents"]`
rewritten — and drop the argument.

The vocabularies are not the same, and that is the point of the move: the
knob's values were raw **ORM paths**, applied with `queryset.order_by` itself,
while a FilterSet's choices are public names it maps through its own
`param_map`. Picking public names is the migration's one decision — they are what the model sees, so give them the words a reader would
use.

## Read-shaping parameters

`page` / `limit` are built in for list selectors and `ordering` comes from the
`filter_set`, but a spec can declare its own parameters that shape what it
returns, as `shapes=` on the spec. The toolset advertises each as a tool
argument, and the kernel hands what the model sent to the spec's presenter
alone, as the call's `PresentationContext.shapes`: no callable of the spec
takes it. That is for a presenter that selects fields or switches between a
summary and a detail rendering. A `null` is read as left out, and a declared
`default` is applied when the model leaves one out, as the tool's schema
advertises.

!!! note "Declared on the spec, never on the toolset"
    A shape is the spec's to declare, so every transport and every mount reads
    the same answer; `SpecToolset` takes no channel declarations of its own,
    and passing `query_params=` or `tool_query_params=` is a
    `TypeError`. For a value the model may not choose, see
    [arguments the server supplies](spec-registry.md#arguments-the-server-supplies).

!!! note "You don't need this for `filter_set`"
    A `SelectorSpec.filter_set`'s fields are already generated into the tool's
    input schema and flow through as ordinary arguments, which the kernel's
    `dispatch` hands the FilterSet as its `filter_data`. So the model can
    filter a list selector with no shape declared at all, and the same goes
    for [ordering](#ordering).

```python
from dataclasses import replace

from django_service_specs import Parameter, Parameters, SpecRegistry

from django_spec_agents.spec_tools import SpecToolset

# A parameter several tools read is one declaration, in each spec's shapes.
FIELDS = Parameter("fields", "string", help="Comma-separated fields to return.")

registry = SpecRegistry()
registry.register(
    "list_orders",
    replace(
        list_orders_spec,
        shapes=Parameters.of(FIELDS, Parameter("detail", "string", default="summary")),
    ),
)
toolset = SpecToolset(registry)
```

A declared shape is one of the call's arguments, so `unknown_arguments` never
flags it; a declared `default` is applied when the model leaves it out or sends
it as `null`, unless it accepts `null` as a value. A shape cannot be named
`page`, `limit` or `ordering`, which the toolset's own pagination and ordering
arguments take.

### On a list tool, a parameter shapes each row

Every list tool returns a page, `{"items": [...], "page": 1, "totalPages": N,
"hasNext": ...}`, but the presenter that reads a shape renders one row at a
time and never sees the envelope. So a selection written against the documented
shape, `fields=items`, asks each row for an `items` field it does not have. The
toolset tells the model this in two places:

- each read-shaping parameter on a list tool has "On a paged result it applies
  to each item in `items`, never to the page envelope (`items`, `page`,
  `totalPages`, `hasNext`)." appended to its description, or as its description
  when it declares none; a retrieve tool's parameter is left as declared;
- the read-shaping line of the instructions the toolset derives says the same,
  "On a tool that returns a page, they apply to each item in `items`, never to
  the page itself.", when some list tool declares a read-shaping parameter.

An `instructions=` override replaces the derived block, and this line with it:
an override written before this sentence existed does not gain it, so add the
advice to your own text if your tools page and take a selection. Changing the
lines you need with [`conventions=`](#changing-what-the-model-is-told) instead
keeps this one, and both sentences are fields there (`read_shaping_on_pages` and
`query_param_on_pages`).

### A selection the presenter rejects

A read-shaping parameter is the one argument used **while the result is
rendered** rather than while the call runs, so it is the one that can fail after
the work is done. When the presenter raises the kernel's
`ServiceValidationError` or `InvalidArguments` while rendering, and the model
supplied a value for at least one of the tool's read-shaping parameters, the
model gets a retry naming the argument:

```text
`fields` was rejected while rendering the result: Unknown field `items`. On a
paged result it applies to each item in `items`, never to the page envelope
(`items`, `page`, `totalPages`, `hasNext`).
```

What follows the colon is the presenter's own message, verbatim. The toolset
never reads a parameter's value, so this works the same for any grammar a
presenter parses. The last sentence is there only on a list tool. With several
parameters supplied, all of them are named, since the presenter does not say
which one it refused. `exception_map=` / `translate_exception` sees the error
first, as on the dispatch path.

The same error **stays loud**, and ends the run, when nothing the model
sent shaped the render: no read-shaping value supplied, an explicit `null`, or a
value that came from a declared `default`. A retry cannot fix a presenter that
fails on its own or a default that is wrong, and would hide the bug behind the
retry budget. Only those two refusals are converted; an `AttributeError` in a
presenter is a bug whatever the model sent.

!!! tip "Use strict selection on tools an agent calls"
    This only works if the presenter says no. Make a name it does not know
    raise `ServiceValidationError`, and the model corrects itself in one retry.
    A tolerant selection, one that drops fields it cannot find, turns the same
    mistake into a page of empty rows, `"items": [{}, {}]`, which is a successful
    result as far as anything downstream can tell. The toolset cannot tell that
    apart from a real answer without knowing the parameter's grammar, so it does
    not try.

## Error handling

The toolset maps the kernel's failure kinds onto the Pydantic-AI model loop:

| Outcome | What the agent sees |
| --- | --- |
| `ServiceValidationError` (bad input), or the kernel's `InvalidArguments` (an argument of the wrong shape, or a Validator's refusal of its value) | `ModelRetry` with the field errors, one `field: message` line each — the model self-corrects. The arguments' shape is checked before the permission check, so a malformed call is handed back before it is authorized |
| A read-shaping value the presenter rejects while rendering | `ModelRetry` naming the argument — see [A selection the presenter rejects](#a-selection-the-presenter-rejects) |
| `ActionUnavailable` (an `Affordance` condition not met) | `ToolFailed` with the reason followed by the rule's code — `The books are closed. (code: books_closed)`; see below |
| `AdditionalInputRequired` (the service wants one more input) | `ModelRetry` with the service's message, ending in the names to supply (``Call this tool again, additionally supplying: `confirmed`.``) — see the warning below |
| `ServiceError` (business rule) | `ToolFailed` with the rule's own message — a failed result the model reads and reports |
| Unresolved instance: a read's or a write's instance selector that finds no row, or raises `DoesNotExist` or Django's `Http404` (as `get_object_or_404` does) | `ToolFailed("not found")`, unless the spec sets `allow_none=True`, when the tool returns `None`. A list selector's `Http404`, usually from a parent it looked up, is `ToolFailed("not found")` whatever `allow_none` says; a list that finds no rows returns `[]`, and a `DoesNotExist` escaping a list selector is an error, not a not-found |
| A dispatch past `dispatch_timeout` | `ToolFailed` — abandoned, with the sentence telling the model to narrow and call again |
| A rendered result over `max_result_bytes` | `ToolFailed` — refused rather than truncated, since a partial payload looks complete |
| Unexpected argument (default `REJECT`) | `ModelRetry` naming the unknown key, in the kernel's words: `note: Unknown argument.` |
| A required argument left out: a required read, or a service's target lookup such as `pk` | `ModelRetry` naming each such parameter left out; a missing Validator field is reported by the Validator when the call runs — see [What a tool asks the model for](#what-a-tool-asks-the-model-for) |
| An invalid item in a `many=True` list | `ModelRetry` with the errors keyed under the list's argument, then the item's index — see [A list as input](#a-list-as-input) |
| An argument sent beside a `many=True` list | As any undeclared key: `ModelRetry` naming it under `REJECT`, dropped under `IGNORE` |
| Non-integer `page` / `limit` | `ModelRetry` — naming what is accepted |
| An `ordering` sent to a list tool that advertises no sort at all | `ModelRetry` — saying the tool has none, rather than letting it fall through as an unknown key |
| A `limit` over `max_page_size`, or a `page` past the last one | Clamped, not refused — the envelope reports the `page` and `totalPages` actually served, so the clamp is visible rather than silent |
| An `OrderingFilter`'s `ordering` sent as one string rather than a list | `ModelRetry` — `ordering: Expected an array.`, as the schema advertises an array |
| An `ordering` outside a `filter_set`'s `OrderingFilter` choices | `ModelRetry` — the FilterSet rejects it, which arrives as the `InvalidArguments` row above |
| A FilterSet `param_map` target that isn't a real column | Django's `FieldError` propagates — an author's error the model cannot correct by picking a different sort, and one that can't be checked at construction without a queryset |
| A denied permission check (class-level `has_permission` **or** object-level `has_object_permission`), raised as the kernel's `NotPermitted`, Django's own `PermissionDenied` raised bare by a service, or a deactivated principal (`PrincipalUnavailable`, refused before any check) | The refusal is raised, logged at `WARNING` naming the tool, and aborts the run — see the caveat below |
| Django REST Framework's `PermissionDenied` or `ValidationError`, raised by a spec | Neither of the above: the exception escapes the call unchanged. A spec library raising them translates them into the kernel's refusals |

!!! warning "A confirmation raised as `AdditionalInputRequired` is not a gate"
    The retry for `AdditionalInputRequired` goes to the model, so the model can
    answer it: a spec that raises `AdditionalInputRequired("Confirm to
    proceed.", schema={"confirmed": ...})` gets its first call back as a retry,
    and the model may re-send the same call with `confirmed=true` and run the
    mutation. No person is asked at any point.

    Treat that confirmation as a prompt for missing input, not as a security
    boundary. An operation that needs a human decision has to be gated by an
    approval mechanism outside the spec: pydantic-ai's deferred-tool approval,
    or this package's `ToolGuard`. `ToolGuard`
    is **off by default**, and a tool named in its `exempt` list stays ungated
    even when it is also in `require_approval`. Every tool built from a mutating
    spec carries `readOnlyHint: False` in its `annotations` metadata (only a
    `SelectorSpec` reads `True`), so an approval policy keyed on that annotation
    covers them.

!!! warning "A tool-failure policy does *not* change the last row"
    "Aborts the run" is what a plain `pydantic_ai.Agent` does: nothing catches
    the exception, so it propagates out of `agent.run`. A tool-failure policy
    normally converts an escaping exception into a failed-tool result and lets
    the run continue — but this package's exempts an authorization
    refusal from exactly that, by default, and `django-ag-ui` inherits the
    exemption. So a denial aborts the run under those hosts too.

    That exemption is deliberate and worth knowing rather than working around:
    converting a denial would leave the run alive with the model free to try the
    next row, and a refusal a model can tell apart from a missing row turns a
    permission boundary into an existence oracle over rows the acting user
    cannot read — inside one turn, spending no retry budget.

    The denial itself is unaffected on every host: nothing is dispatched and no
    data is rendered.

!!! danger "Your own exception has to *be* a `ServiceError`"
    Every row above is matched by type. A service that raises its own error
    class gets none of this unless that class derives from the kernel's
    `ServiceError`:

    ```python
    from django_service_specs import ServiceError


    class BookingError(ServiceError):  # not Exception
        """Something the booking rules refuse."""
    ```

    Deriving from `Exception` is the ordinary thing to reach for, and the
    failure is silent in a way worth spelling out, because it is not confined to
    the agent. The same exception escapes an HTTP view as a **500 for what is
    plainly a 409**, escapes MCP as a protocol error, and aborts an agent run
    rather than settling the call as failed. Nothing warns — not at
    declaration, not at mount, not at dispatch.

    Two separate projects made this exact mistake and neither test suite could
    see it, because every write test asserted a path that succeeds. If a project
    genuinely cannot change its exception's base class, `exception_map=` is the
    other door: it takes the type and returns what the model should be told.

### A refused affordance names its code

The kernel's dispatch raises `ActionUnavailable` when one of a spec's `Affordance`
conditions is not met, with the affordance's `reason` as the message and its
`code` as an attribute. `ToolFailed` carries a message and nothing else, and that
message is also what a transport forwards as the tool result, so the toolset puts
both into it — the reason first, then the code, labelled:

```python
from django_service_specs import Affordance, DataclassValidator, ServiceSpec, Unrestricted


def post_invoice(*, data, user):
    """Post an invoice to the current period."""
    ...


post_invoice_spec = ServiceSpec(
    service=post_invoice,
    validator=DataclassValidator(InvoiceIn),
    permissions=[Unrestricted()],
    affordances=[
        Affordance(
            code="books_closed",
            reason="The books are closed.",
            when=lambda: Period.current().is_open,
        ),
    ],
)
```

While the period is closed the tool is [left out of the
catalog](#an-operation-that-is-unavailable-right-now-is-left-out), so a run reaches
this refusal only through a listing that went stale: the step was offered the tool,
and the period closed before the call arrived. A condition on the row is refused
the same way, and it is never asked when the catalog is listed. The failed tool
result's content, which is what the model reads and what a transport streams, is:

```text
The books are closed. (code: books_closed)
```

The code is the part that connects the refusal to what the model may already have
read. A selector naming the same spec in its own `affordances`
(`affordances={"post_invoice": post_invoice_spec}`) answers it on each row it
renders, under `affordances.post_invoice`, as
`{"available": false, "code": "books_closed", "reason": "The books are closed."}`
— and the reason is a sentence a project may reword while the code stays put. The
conventions block `get_instructions` returns tells the model a refusal may end
this way. A `ServiceError` or `ServiceConflict` raised by hand has
no code and keeps its message exactly as written.

**The suffix is written for the model and for a person reading a tool card, not
for a program.** It is meant to read the same on every agent transport serving
these specs, and its wording is not an interface. A program that branches on the
code reads `.code` off the exception instead: the `ActionUnavailable` is the
`ToolFailed`'s `__cause__`, and `translate_exception` receives it before the
toolset's own handling runs. A handler returned from there, or registered in
`exception_map` for `ActionUnavailable` or any class above it, replaces the
sentence along with the rest of the default.

### Why the failed rows raise instead of returning

Every `ToolFailed` row above is raised rather than returned as a
`{"error": "..."}` dict. The model reads the same sentence either way, so the
choice is not about what it sees — it is about what everything *else* sees.
Pydantic-AI marks an ordinary return `outcome="success"` on the resulting
`ToolReturnPart`; only a raised `ToolFailed` marks it `"failed"`. Returning the
dict would make a refusal indistinguishable from an answer to a log, an audit
record, or a transport streaming the call to a browser, all of which would have
nothing but the payload's wording to go on.

`ToolFailed` keeps the two properties a returned dict would have: it spends none
of the tool's retry budget, and it does not end the run. It also prepends no
correction instructions, which `ModelRetry` does — right for a bad argument, and
wrong for a conflict the model cannot argue with.

If a failure is one your project would rather express as an ordinary result,
`exception_map` still takes a handler that **returns** a value, and a returned
value is still marked `"success"`.

Each `ModelRetry` row consumes one unit of the tool's retry budget: after
`max_retries` failed attempts (default `1`, pydantic-ai's function-tool
default) the run aborts with `UnexpectedModelBehavior`. Raise it for models
that need more attempts to converge:

```python
toolset = SpecToolset(specs, max_retries=3)
```
