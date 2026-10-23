# Specs as tools

Expose services and selectors declared as specs as a
[Pydantic-AI](https://ai.pydantic.dev) toolset, so a plain `pydantic_ai.Agent`
can call them as tools — **no MCP server and no AG-UI bridge** in the path. A
spec is anything
[`django-service-specs`](https://github.com/Artui/django-service-specs)
compiles: one of its own `ServiceSpec` or `SelectorSpec`, or a value its
`kernel_spec` compiles to one.

## What this is

A single thin adapter, [`SpecToolset`](reference.md#django_spec_agents.spec_tools.SpecToolset),
that turns a `name -> spec` mapping into agent tools. Every call runs on the
kernel, on the compiled spec, so a spec is served the same way whichever library
declared it:

- **`dispatch`**, the kernel's, authorizes the call and executes the service
  or selector: the class-level check of the spec's permissions before
  anything is called, the object-level one on the resolved target, and
  validation, instance resolution and the output-selector re-fetch around
  them. Authorization is part of the dispatch, so no adapter can skip it;
- **the run's `call_context`** carries the request the run arrived on, if any.
  Where it carries none, and no `call_seeds` override returns one, a selector
  or provider declaring `request` or `view` is handed `None`, while a
  permission check reads the principal and, where it declares `arguments`,
  the call's arguments;
- **`spec_input_schema`**, the kernel's, derives each tool's parameter schema
  from the compiled spec, the argument set `dispatch` checks, and
  **`spec_output_schema`** its `return_schema`, projected for the audience;
- **`paginate_output`**, the kernel's, slices a list selector into the page the
  tool returns, the same shaper the MCP transport uses;
- **`present_for_audience`**, the kernel's, renders the result through the
  spec's presenter and applies the field markings.

The result is that an agent driving these tools sees exactly what an HTTP client
would: the same validation errors, the same permission checks (class-level
before anything is called, and object-level on the resolved row), the same rendered payloads —
without the network hop, and without standing up an MCP server.

## How it compares

If you already run an MCP server, `django-ag-ui`'s `DRFMCPToolset` exposes its
tools to Pydantic-AI through the in-process MCP surface. `SpecToolset` is for the
case where you want the specs as tools **without** any MCP layer: the `[specs]`
extra installs it on `django-service-specs` and `pydantic-ai-slim` alone, and
the toolset imports nothing from Django REST Framework.

## Next steps

- [Quickstart](quickstart.md) — wire a toolset to an agent.
- [Shared spec registry](spec-registry.md) — declare your specs once when
  more than one transport exposes them.
- [Reference](reference.md) — the public API.
