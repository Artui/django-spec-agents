# Tools

## Registering one

A tool is a plain typed callable registered on a `ToolRegistry`:

```python
from django_spec_agents import ToolCategory, ToolRegistry, tool

registry = ToolRegistry()


@tool(registry, category=ToolCategory.UI_READ)
def find_order(order_id: int) -> dict:
    """Look up an order by id."""
    ...
```

The name defaults to the function's name, and the description to the first
paragraph of its docstring; both can be overridden. Registering a name twice
raises `ValueError` rather than shadowing.

The name you register is the name the model is offered, and the one the
[tool guard](policy.md), the audit record and the tool catalog use. An explicit
`description=` is what the model reads in place of the docstring, an empty one
excepted: some supported Pydantic-AI versions treat it as none. A tool with
no `description=` is offered exactly what Pydantic-AI derives from the
docstring, which keeps more than the first paragraph the registry's own
`description` holds. A `description=` identical to
that first paragraph is indistinguishable from none, so the model gets the
fuller text.

`ToolRegistry` state lives on the **instance** — a transport holds one, and
tests build a fresh one per scenario. There is no global registry.

## Tools must be typed

The registry derives each tool's JSON Schema from the signature at registration
time, so **every parameter and the return need a concrete annotation**. There is
no `**kwargs: Any` escape hatch: an untyped parameter has no schema, and the
model would be guessing.

Sync and async callables both work; the registry dispatches either.

## Metadata, and where it surfaces

`@tool` takes four pieces of metadata beyond name and description. All of them
end up as JSON-Schema extension keys, because a schema is the one channel that
reaches a client without inventing a side channel:

| Argument | Schema key | What it is for |
| --- | --- | --- |
| `destructive=True` | `x-destructive` | This tool mutates. AG-UI has no native destructive-tool concept, so it is stamped at the schema root and read client-side to gate execution behind a confirmation — and server-side by the [tool guard](policy.md), for a tool whose schema is the only place its author said so. |
| `category=` | `x-category` | Coarse grouping (`ToolCategory`) so a frontend can group or filter, or a system prompt can reason about capability classes. |
| `confirm="Activate this project?"` | `x-confirm` | The confirmation prompt shown instead of a generic "Run *tool*?". |
| `summary="Query orders"` | `x-summary` | A short display label shown on a tool-call card instead of the raw tool name. |

**`category` does not gate anything.** It is advisory metadata for grouping and
policy; `destructive` is the flag that drives the approval gate.

There is a fifth key that is *not* a schema stamp: `DESTRUCTIVE_METADATA_KEY`
(`"django_spec_agents.destructive"`) rides pydantic-ai's `ToolDefinition`
metadata rather than the schema, because metadata is the channel a *bridge*
controls where the schema belongs to the tool's author. It is read
**server-side** at `prepare_tools` time by the [tool guard](policy.md), which
needs destructiveness for tools whose flag doesn't come from the `@tool`
registry at all — a bridged MCP tool, whose `readOnlyHint` and `destructiveHint`
annotations the bridge maps onto this key.

A registry tool's own `destructive=True` never travels either channel: it stays
on the spec, and the guard reads it from the registry directly. What reaches the
guard through the schema is the case where `build_input_schema` was used
[on its own](#deriving-a-schema-on-its-own) and the tool attached through
`toolsets=`.

## The tool catalog

Server-side tools execute server-side, so their JSON Schema never reaches the
browser — which means a web component can't read an `x-summary` off it. That is
what `build_tool_catalog` is for: a list of `{"name", "summary", "description"?}`
entries a frontend fetches to label tool-call cards.

`summary` is always present, resolved through a fallback chain — for registry
tools, `@tool(summary=…)` then a prettified name; for the tools of the server
passed as `mcp_server=`, `display_name` then `title` then a prettified name.
`drf_mcp_server=`, the keyword's old name, is deprecated and removed in the next
minor; it warns `DeprecationWarning`, and passing both raises `TypeError`.

```python
from django_spec_agents import build_tool_catalog

catalog = build_tool_catalog(registry, mcp_server=server, service_specs=specs)
```

### What the catalog does not cover

**Only the sources above.** A tool reaching the agent through a transport's
`capabilities=` / `toolsets=` — an arbitrary `AbstractToolset` attached directly
— is not listed, and its card falls back to a prettified tool name.

**This is a stated boundary, not a gap waiting to be filled.** Pydantic-AI's
enumeration is `AbstractToolset.get_tools`, which is `async` and takes a
`RunContext`; the catalog is built at configuration time, from a view, with no
run in sight. Enumerating "the toolsets we recognise" and skipping the rest
would produce a catalog that *looks* complete and is not — the failure this
stack spends its time removing, traded for cosmetics.

**Route spec tools through a covered source instead.** A `SpecToolset` or
`SpecCapability` passed to django-ag-ui's `service_specs=` (0.30+) is attached
as itself *and* enumerated here, so it keeps its labels.

An unlabelled card is a **degraded label, not a broken call**: the tool executes
normally and the frontend shows a prettified name.

## Deriving a schema on its own

`build_input_schema` is the same derivation the registry runs, exposed for
projects that want a tool's schema outside the registration flow:

```python
from django_spec_agents import build_input_schema

schema = build_input_schema(find_order, destructive=False, category=ToolCategory.UI_READ)
```

A leading `ctx: RunContext[...]` parameter is **not** a tool argument and is
left out of the schema: pydantic-ai fills it from the run, so advertising it
would ask the model to invent a value for something it cannot supply. Dispatch
follows the same rule — `registry.call(name, arguments, ctx=ctx)` binds it from
the caller, and a tool that declares one and is dispatched without a `ctx`
raises rather than being handed `None`.

### What the hand-built schema carries

The default derivation maps `str`, `int`, `float`, `bool`, `list[T]` (and
`tuple`, `set`, `frozenset`), `dict`, `X | None` and other unions. Anything else,
a model or an enum say, is an empty fragment: it constrains nothing and stays
wire-valid.

`Annotated[T, ...]` is read as `T` plus what its metadata maps to:

| Metadata | Schema keys |
| --- | --- |
| `Field(description=...)`, `Field(title=...)`, `Field(examples=...)` | `description`, `title`, `examples` |
| `ge`, `gt`, `le`, `lt`, `multiple_of` (a `Field`'s, or `annotated_types`' `Ge`, `Interval` and so on) | `minimum`, `exclusiveMinimum`, `maximum`, `exclusiveMaximum`, `multipleOf` |
| `min_length`, `max_length` (or `annotated_types.Len`) | `minLength` / `maxLength` on a string, `minItems` / `maxItems` on a list, `minProperties` / `maxProperties` on a dict, nothing on a union |
| `pattern`, a string or a compiled `str` pattern | `pattern` |

A `title` appears only when the `Field` passes one. Other `Field` arguments
(`alias`, `json_schema_extra`, `deprecated`) are not carried, nor is any other
metadata, such as a plain string.

A `Field` given as the parameter's default, `n: int = Field(ge=1)`, reads as one
inside `Annotated` does, after the annotation's own metadata, so its keys win. A
`Field` with no default makes the parameter required, as pydantic does:
`ToolRegistry.call` binds the arguments as given, so an omitted `n` would reach
the tool as the `FieldInfo` itself. A `Field` with a default shows it as
`default`, in JSON mode (left out when it does not serialise), and a
`default_factory` makes the parameter optional with no `default` shown. A plain
default, `n: int = 1`, makes the parameter optional and is not shown. A string or forward reference
nested in an annotation, `Annotated["int", Field(ge=1)]` or `list["int"]`, is
resolved in the globals the top level is evaluated in: the module of the function
`inspect.signature` reads, through a decorator's `__wrapped__`, a
`functools.partial`'s `func` or a callable instance's `__call__`. One that does
not resolve is an empty fragment, as an unknown type is. Every value carried serialises to
JSON: a bound that is not an `int` or `float` (a `Decimal`, a `date`), or is
infinite or NaN, is left out, and `examples` are rendered as pydantic renders
them (a `Decimal` as its string), or left out when they do not serialise or
hold an infinite or NaN float.

### Choosing a strategy

`schema_strategy=` picks the derivation, on `build_input_schema` for one call
and on `ToolRegistry(schema_strategy=...)` for every binding the registry
derives. It takes a `SchemaStrategy`:

- `SchemaStrategy.HAND_BUILT`, the default: the mapping above.
- `SchemaStrategy.PYDANTIC`: a pydantic model over the same parameters (the
  `RunContext` and `*args` / `**kwargs` left out), with extra keys forbidden, and
  its `model_json_schema()`. It carries whatever pydantic does, models and enums
  included.

Under either, a property is keyed by its parameter's name, never by a `Field`
alias (`alias=` or `validation_alias=`), because `ToolRegistry.call` binds the
arguments by name: `s: Annotated[str, Field(alias="other")]` is advertised as
`s`, and a payload keyed `other` is refused. The pydantic strategy passes
`by_alias=False`, so a nested model's fields are keyed by name too.

The `x-*` keys are stamped at the root under either. The strategy shapes the
schema `build_input_schema` returns and `ToolBinding.input_schema`; the schema the
agent offers the model for an `@tool` tool is pydantic-ai's own under both.

For this tool:

```python
def find_order(
    n: Annotated[int, Field(description="How many.", ge=1)],
    note: str | None = None,
) -> list[Order]: ...
```

the hand-built strategy gives a flat schema with no titles, `nullable` for
`X | None`, and no defaults but a `Field`'s:

```json
{
  "type": "object",
  "properties": {
    "n": {"type": "integer", "description": "How many.", "minimum": 1},
    "note": {"type": "string", "nullable": true}
  },
  "additionalProperties": false,
  "x-category": "other",
  "required": ["n"]
}
```

and `schema_strategy=SchemaStrategy.PYDANTIC` gives pydantic's, with a `title`
on the root (the function's name) and on each property, `anyOf` with a `null`
branch for `X | None`, the default, and a nested model or enum under `$defs`,
reached by `$ref`:

```json
{
  "additionalProperties": false,
  "properties": {
    "n": {"description": "How many.", "minimum": 1, "title": "N", "type": "integer"},
    "note": {
      "anyOf": [{"type": "string"}, {"type": "null"}],
      "default": null,
      "title": "Note"
    }
  },
  "required": ["n"],
  "title": "find_order",
  "type": "object",
  "x-category": "other"
}
```

**Where they differ in failing.** Under both, an annotation that does not
evaluate at the top level, `x: Missing` (in a module with `from __future__
import annotations`, any unresolvable name), raises `NameError` from
`inspect.signature`, so at registration for a `ToolRegistry`. Past that, the
hand-built strategy does not raise on a type: what it cannot map, or a nested
reference that does not resolve, is an empty fragment. The pydantic strategy raises at
derivation, so at registration for a `ToolRegistry`, where pydantic does: a type
it cannot build a schema for (`PydanticSchemaGenerationError`), a reference that
does not resolve (`NameError`), and a parameter pydantic would not keep as a
field, a leading underscore or `model_config` (`TypeError`, rather than a schema
silently missing an argument). A default that is not JSON-serialisable is left
out of its schema, with pydantic's warning.

Pick the hand-built strategy for a schema that stays as it is across pydantic
releases and does not fail a registration on a type it cannot map; pick
pydantic's when a client should see what the agent's own validation sees,
nested models and enums included.

See the [registry reference](reference/registry.md) for the full signatures.
