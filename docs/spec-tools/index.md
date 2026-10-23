# Specs as tools

Expose services and selectors declared as specs as a
[Pydantic-AI](https://ai.pydantic.dev) toolset, so a plain `pydantic_ai.Agent`
can call them as tools — **no MCP server and no AG-UI bridge** in the path. A
spec is anything
[`django-service-specs`](https://github.com/Artui/django-service-specs)
compiles: one of its own `ServiceSpec` or `SelectorSpec`, or a
[`djangorestframework-services`](https://github.com/Artui/djangorestframework-services)
spec, which compiles to one.

## What this is

A single thin adapter, [`SpecToolset`](reference.md#django_spec_agents.spec_tools.SpecToolset),
that turns a `name -> spec` mapping into agent tools. Every call runs on the
kernel, on the compiled spec, so a spec declared with either library is served
the same way:

- **`dispatch`**, the kernel's, authorizes the call and executes the service
  or selector: the class-level check of the spec's permissions before
  anything is called, the object-level one on the resolved target, and
  validation, instance resolution and the output-selector re-fetch around
  them. Authorization is part of the dispatch, so no adapter can skip it;
- **the run's `call_context`** carries the request the run arrived on, if any.
  Where it carries none, a drf-services spec's permission and presenter
  adapters build an offline request around the principal, so
  `permission_classes` and serializer context providers keep working, while a callable declaring
  `request` or `view` is handed `None`;
- **`spec_input_schema`**, the kernel's, derives each tool's parameter schema
  from the compiled spec, the argument set `dispatch` checks, and
  **`spec_output_schema`** its `return_schema`, projected for the audience;
- **`paginate_output`**, the kernel's, slices a list selector into the page the
  tool returns, the same shaper the MCP transport uses;
- **`present_for_audience`**, the kernel's, renders the result through the
  spec's presenter and applies the field markings.

The result is that an agent driving these tools sees exactly what an HTTP client
would: the same validation errors, the same permission checks (for a
drf-services spec, both class-level `has_permission` and object-level
`has_object_permission` on the resolved row), the same rendered payloads —
without the network hop, and without standing up an MCP server.

## How it compares

If you already run an MCP server, `django-ag-ui`'s `DRFMCPToolset` exposes its
tools to Pydantic-AI through the in-process MCP surface. `SpecToolset` is for the
case where you want the specs as tools **without** any MCP layer: the `[specs]`
extra installs it on `django-service-specs` and `pydantic-ai-slim`, and still
brings Django REST Framework and `djangorestframework-services`, of which the
toolset imports a few names (its exception classes and the schema registry a
filter type needs).

## Next steps

- [Quickstart](quickstart.md) — wire a toolset to an agent.
- [Shared spec registry](spec-registry.md) — declare your specs once when
  more than one transport exposes them.
- [Reference](reference.md) — the public API.
