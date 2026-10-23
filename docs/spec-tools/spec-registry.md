# Declaring specs once, across transports

If the agent is the only place your project exposes its specs, keep passing a
plain dict — this page buys you nothing.

It pays off when the **same specs** are also exposed elsewhere: over MCP with
[`djangorestframework-mcp-server`](https://github.com/Artui/djangorestframework-mcp-server),
or as ordinary HTTP views. Each transport has to be told which specs it exposes,
so the list gets written once per transport and the copies drift — a spec is
added to the MCP wiring and forgotten in the agent's, or the same operation is
named `create_order` in one place and something else in the other.

`SpecRegistry` is the one declaration
site. `SpecToolset` and `SpecCapability` accept one **anywhere they accept a
mapping** — there is no separate constructor to learn.

## Declare once

```python
# orders/registry.py
from django_service_specs import SpecRegistry

registry = SpecRegistry()


# orders/apps.py
from django.apps import AppConfig


class OrdersConfig(AppConfig):
    name = "orders"

    def ready(self) -> None:
        from orders import specs
        from orders.registry import registry

        registry.register("list_orders", specs.list_orders, tags=("read", "public"))
        registry.register("refund_order", specs.refund_order, tags=("write", "admin"))
```

## Hand it to the agent

```python
from pydantic_ai import Agent

from django_spec_agents import AgentDeps
from django_spec_agents.spec_tools import SpecToolset
from orders.registry import registry

agent = Agent(model, deps_type=AgentDeps, toolsets=[SpecToolset(registry)])
```

That is the whole change. Each entry becomes one tool, named as it is in the
registry, in registration order.

The registry is django-service-specs', and it accepts any spec that compiles to
one of the kernel's, and so does the toolset: a django-service-specs
`ServiceSpec` or `SelectorSpec`, or any value the kernel's `kernel_spec`
compiles to one. The toolset reads every spec through `kernel_spec`, so it asks
the compiled spec what to advertise and how to call it, whichever library
declared it. A value that compiles to no spec raises `TypeError` naming the
tool when the toolset is built, in the kernel's words.

## A spec declared on the kernel

A django-service-specs spec needs no DRF. Its permissions are the kernel's
`PermissionCheck`s, and **its arguments are its declared `reads`**: the kernel
reflects nothing off a native callable's signature, so each argument the model
may send is a `Parameter`, and the callable takes it by name beside the pool
seeds (`user` here). A native spec declaring no reads advertises no argument of
its own, and a call sending one is handed back as `name: Unknown argument.`

```python
from django_service_specs import (
    Parameter,
    Parameters,
    PermissionCheck,
    SelectorKind,
    SelectorSpec,
    ServiceSpec,
)

from django_spec_agents.spec_tools import SpecToolset
from orders.models import Order


class SignedIn(PermissionCheck):
    def has_permission(self, principal, spec) -> bool:
        return bool(principal is not None and principal.is_authenticated)


def orders_in_status(*, user, status: str) -> list[dict]:
    """The caller's orders in one status."""
    return list(Order.objects.filter(owner=user, status=status).values("id", "status"))


def set_order_note(*, user, pk: int, note: str) -> dict:
    """Write the note on one of the caller's orders."""
    Order.objects.filter(owner=user, pk=pk).update(note=note)
    return {"id": pk, "note": note}


list_orders = SelectorSpec(
    kind=SelectorKind.LIST,
    selector=orders_in_status,
    reads=Parameters.of(Parameter("status", "string", default="open")),
    permissions=[SignedIn()],
)
note_order = ServiceSpec(
    service=set_order_note,
    reads=Parameters.of(
        Parameter("pk", "integer", required=True),
        Parameter("note", "string", required=True),
    ),
    permissions=[SignedIn()],
)

toolset = SpecToolset({"list_orders": list_orders, "note_order": note_order})
```

`list_orders` advertises `status`, defaulting to `open`, beside the `page` and
`limit` every list tool takes, and returns a page of the dicts its selector
returns. `note_order` requires `pk` and `note`. A registry takes these specs as
it takes any other, and the toolset serves them from either.

## A registry or its mapping

`SpecToolset(registry)` and `SpecToolset(registry.specs())` build the same
tools: the mapping is the flattened `name -> spec` view, and everything the
toolset reads is on the spec. Pass the registry when you have one, so a
filtered view of it can feed each toolset.

The same goes for the capability:

```python
from django_spec_agents.spec_tools import SpecCapability

AgentConfig(capabilities=[SpecCapability(registry)])
```

## Project several toolsets from one declaration

`by_tag` and `subset` each return a **new** registry holding a snapshot, so one
declaration site can feed several toolsets with no shared state:

```python
reads = SpecToolset(registry.by_tag("read"), id="reads")
admin = SpecToolset(registry.by_tag("admin"), id="admin")

agent = Agent(model, deps_type=AgentDeps, toolsets=[reads, admin])
```

With capabilities, give each its own `id` — the id keys `defer_loading`'s
catalog entry, so two capabilities sharing one would collide:

```python
AgentConfig(
    capabilities=[
        SpecCapability(
            registry.by_tag("read"),
            id="reads",
            description="Read-only order and customer lookups.",
        ),
        SpecCapability(
            registry.by_tag("admin"),
            id="admin",
            description="Account administration: suspend, refund, reassign.",
            defer_loading=True,
        ),
    ]
)
```

Here `defer_loading` hides the admin tools behind Pydantic-AI's native
`load_capability` tool until the model asks for them — worth doing when the
admin surface is large and rarely needed.

**Give every deferred capability a `description`.** It is the line the model
picks from: the catalog renders `- {id}: {description}` when there is one and a
bare `- {id}` when there is not, so a few undescribed capabilities leave the
model choosing between names alone — and it will either guess or load all of
them, which is the cost deferring was meant to avoid. `description` names the
*capability*; the separate `descriptions` mapping relabels individual tools.

## What the spec declares for an agent

A spec declares what a caller with **no HTTP request** has to be told, because
the URLconf and query string tell an HTTP one for free: `reads=` names the route
captures and query values it reads, and an `OutputField`'s `marking=` says how
the field is presented to an agent. Both are the spec's: each read is one of its
parameters, and the markings are part of its output.

```python
from dataclasses import replace

from django_service_specs import Parameter, Parameters

registry.register(
    "list_orders",
    replace(
        specs.list_orders,
        reads=Parameters.of(Parameter("tenant_pk", "integer", required=True)),
        shapes=Parameters.of(Parameter("fields", "string")),
    ),
    tags=("read", "public"),
)
```

`replace` is for a spec declared elsewhere; a spec you write declares `reads=`
and `shapes=` in its own constructor. `SpecRegistry.register` takes no contract of its own:
the spec is the one place it is said.

`SpecToolset` reads them off the compiled spec, and an MCP server's
`register_specs` reads the same spec. That is the point of putting them there:
an operation needs the *identical* description of the absent request whichever
agent transport calls it, which is why a toolset takes no channel declarations
of its own:
`SpecToolset` has no `query_params=`, `tool_query_params=`, `url_kwargs=` or
`tool_url_kwargs=`, and passing one is a `TypeError`.
A field's marking is part of the spec's output declaration, so every projection
of the spec carries it, whichever transport or mapping it is reached through.

**A registry and `registry.specs()` build the same tools.** The tool's schema
is the compiled spec's, the spec's `reads` are advertised, a read's `default`
reaches a call that leaves it out or sends `null`, and a value the model sends
under one of those names is admitted and handed to the spec as the kernel hands
any caller's. A read declared `required=True` and sent as `null` is handed back
as `name: This field is required.`, the kernel's check. A read declared `required=True` with a `default` is not refused: the
default answers a call that leaves it out, as the kernel reads it. A native
read declared `nullable` keeps a `null` sent under it as the value.

Every read the spec declares is checked against the
[pool seeds](quickstart.md#project-pool-seeds) when the toolset is built,
whichever source it came from: a read named after a registered seed, at the top
level or on a service's target lookup, is refused there, naming the tool. A
parameter in `shapes=`, which no callable takes, may not be named `page`,
`limit` or `ordering`, which a list tool takes for itself. A read named
`ordering` that a callable takes is an argument like any other, advertised and
left in the call.

## Arguments the server supplies

Some arguments are not the model's to choose. An agent run scoped to one
project should not be able to list another project's rows by sending a
different `project_pk`, and a model asked for an identifier it cannot know
tends to invent one or send the parameter's name. `tool_server_arguments`
answers for such names per tool, with django-service-specs'
[`ServerArguments`][django_service_specs.types.server_arguments.ServerArguments]:

```python
from django_service_specs import ServerArguments

toolset = SpecToolset(
    registry,
    tool_server_arguments={
        "list_document_vendors": ServerArguments(
            values={"project_pk": str(document.project_id)},
            withheld=frozenset({"draw_request_pk"}),
        ),
    },
)
```

- **A bound name** (`values`) carries the server's value on every call. It
  satisfies a `required` parameter, and every reader of the call sees the one
  value: a `kwargs=` provider taking its name is handed it, and the selector
  receives it as its parameter.
- **A withheld name** is absent from every call, so the read's declared default,
  or the callable's own, answers. Withholding the other names of an either-or
  scope is how a tool taking a project *or* a draw request is held to the
  project.
- **Neither is advertised.** Both leave the tool's `properties` and `required`,
  and the instructions name neither. If the model sends one anyway, its value
  is dropped before the call is checked, never refused, so a guess costs no
  retry, and the toolset logs the tool and the names at `WARNING`, never the
  values.

Each name must be one the tool's spec declares: a selector or target-lookup
parameter, or one of its `reads`. The toolset checks every binding when it is
built, with django-service-specs' `check_server_arguments` and the toolset's
own `pool_seeds`, and raises `ImproperlyConfigured` naming the tool for a name
the spec does not declare, a name a pool seed or a typed `kwargs=` provider
fills, a withheld name no call could do without, or a bound value its
parameter refuses. A key naming a tool the toolset does not expose raises
`ValueError`, as every per-tool mapping does.

The bindings are fixed when the toolset is built, like its tool definitions.
For a per-run scope, build a toolset per run, from the run's scope. A tool
counts as scoped by the run only when the run binds one of the scope names it
reads: its other optional scope reads are then withheld too, so the model
cannot widen the scope through a sibling, while a required one the run does
not bind stays the model's to send, since withheld no call could supply it. A
tool reading only scope names the run does not bind gets no binding and keeps
them all:

```python
SCOPE_NAMES = frozenset({"project_pk", "draw_request_pk", "document_pk"})


def scope_bindings(registry, scope):
    """Each tool the run's scope reaches: the bound names, and optional siblings withheld."""
    bindings = {}
    for entry in registry.all():
        reads = [read for read in entry.spec.reads if read.name in SCOPE_NAMES]
        values = {read.name: scope[read.name] for read in reads if read.name in scope}
        if not values:
            continue
        bindings[entry.name] = ServerArguments(
            values=values,
            # A required read stays the model's: withheld, no call could supply it.
            withheld=frozenset(
                read.name for read in reads if read.name not in values and not read.required
            ),
        )
    return bindings


toolset = SpecToolset(registry, tool_server_arguments=scope_bindings(registry, scope))
```

`SpecCapability` takes the same keyword.

## What stays per toolset

The rest of `SpecToolset`'s signature, because it is genuinely this mount's:
`get_user`, `unknown_arguments`, `max_retries`, the result-size and page
ceilings. A publicly exposed MCP endpoint and an in-process toolset carry
different risk, so one shared number would be a regression rather than a
simplification.

### Moving a channel declaration off the toolset

A toolset that declared a read at the mount declares it on the spec instead, as
`reads=` and `shapes=` (see
[what the spec declares for an agent](#what-the-spec-declares-for-an-agent)).
A read's `default` is only a default: the model can override it, and it stays
advertised. To keep a value the model should not choose, use
[`tool_server_arguments`](#arguments-the-server-supplies).

## Names

Registry names are free-form, but tool names are not: model providers constrain
them to `[a-zA-Z0-9_-]{1,64}`. A registry name outside that shape raises when the
toolset is built, rather than failing later at the provider boundary. If you
share a registry with a transport that has looser naming, keep the names
tool-safe.
