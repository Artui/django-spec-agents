# Reference

`django_spec_agents.spec_tools` exports three symbols: `SpecToolset`,
`SpecCapability` and `AgentConventions`. A spec declares what it reads with
drf-services' `QueryParam` and `UrlKwarg`, imported from
`rest_framework_services` and rendered below for reference. The deps a run
passes are the package's one
[`AgentDeps`](../reference/agent.md#django_spec_agents.AgentDeps), and the
doubles for driving a toolset through a real run loop are in
`django_spec_agents.testing`.

## `SpecToolset`

::: django_spec_agents.spec_tools.SpecToolset

### Subclassing `SpecToolset`

A project scoping an assistant to what one run may reach subclasses
`SpecToolset`, and these are the parts of it a subclass builds on. They are
public.

- **`get_tools(ctx)` and `call_tool(name, tool_args, ctx, tool)`**, pydantic-ai's
  toolset methods. Override `get_tools` to narrow the catalog a run is offered
  and `call_tool` to wrap a call, each delegating to `super()`: the run loop
  offers the model what `get_tools` returns and dispatches every call through
  `call_tool`.
- **`specs` and `id`**, read-only properties that answer at configuration time,
  with no run: `specs` is every tool the toolset was built with, whatever a
  `get_tools` override offers a given run.
- **The five seams**, each receiving the live `RunContext`:
  [`call_seeds`][django_spec_agents.spec_tools.SpecToolset.call_seeds] and
  [`translate_exception`][django_spec_agents.spec_tools.SpecToolset.translate_exception]
  cover the front half of a call,
  [`shape_page`][django_spec_agents.spec_tools.SpecToolset.shape_page],
  [`render_output`][django_spec_agents.spec_tools.SpecToolset.render_output] and
  [`enforce_result_bytes`][django_spec_agents.spec_tools.SpecToolset.enforce_result_bytes]
  the back half.
- **[`AgentDeps.call_context`][django_spec_agents.AgentDeps.call_context]**, the
  run's context, of which every call is handed its own copy.

An attribute whose name starts with an underscore is not part of it, and a
minor release may rename one.

## `SpecCapability`

::: django_spec_agents.spec_tools.SpecCapability

## `AgentConventions`

::: django_spec_agents.spec_tools.AgentConventions

## `QueryParam`

::: rest_framework_services.types.query_param.QueryParam

## `UrlKwarg`

::: rest_framework_services.types.url_kwarg.UrlKwarg

## `testing`

Test doubles for driving a toolset through a real `Agent` run loop. See
[Running from a worker](background-runs.md#testing-it).

::: django_spec_agents.testing
