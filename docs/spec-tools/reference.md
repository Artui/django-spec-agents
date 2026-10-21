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

::: django_spec_agents.spec_tools.testing
