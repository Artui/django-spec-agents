# django-spec-agents

[![CI](https://github.com/Artui/django-spec-agents/workflows/tests/badge.svg)](https://github.com/Artui/django-spec-agents/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/django-spec-agents.svg)](https://pypi.org/project/django-spec-agents/)
[![Python versions](https://img.shields.io/pypi/pyversions/django-spec-agents.svg)](https://pypi.org/project/django-spec-agents/)
[![Django versions](https://img.shields.io/pypi/djversions/django-spec-agents.svg)](https://pypi.org/project/django-spec-agents/)
[![Docs](https://img.shields.io/badge/docs-artui.github.io-blue.svg)](https://artui.github.io/django-spec-agents/)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/Artui/django-spec-agents/gh-pages/coverage.json)](https://github.com/Artui/django-spec-agents/actions/workflows/tests.yml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License](https://img.shields.io/pypi/l/django-spec-agents.svg)](https://github.com/Artui/django-spec-agents/blob/main/LICENSE)

[Pydantic-AI](https://ai.pydantic.dev) agents for Django, in two halves. The
**agent host** takes an `AgentConfig` and a tool registry and returns a built
`pydantic_ai.Agent`, with an audit trail over every tool call, a server-side
approval gate for destructive tools, a failure policy, conversation and attachment
stores, and an opt-in store app. It reads no Django settings of its own: a
transport such as [django-ag-ui](https://github.com/Artui/django-ag-ui) owns its
settings, builds the config and hands it down. **`spec_tools`**, behind the
`[specs]` extra, serves specs to an agent as tools, whether declared on
[django-service-specs](https://github.com/Artui/django-service-specs) or with
[djangorestframework-services](https://github.com/Artui/djangorestframework-services), in process, with each spec's own validation,
permissions and rendering: `SpecToolset`, and `SpecCapability` over it.

The host was published as `django-pydantic-agent` and `spec_tools` as
`djangorestframework-pydantic-ai`. This package carries both under one import
path, `django_spec_agents`, and the root never imports `spec_tools`, so the host
installs and runs without the extra.

## Install

```bash
pip install django-spec-agents            # the agent host
pip install "django-spec-agents[specs]"   # and specs as agent tools
```

Python 3.10 or later and Django 4.2 or later. `[specs]` adds Django REST
Framework, `djangorestframework-services` and `django-service-specs`. The other
extras: `[drf-mcp]` bridges a `djangorestframework-mcp-server` registry as a
toolset, `[harness]` adds the `pydantic-ai-harness` capabilities, `[filter]` adds
`django-filter` for filtered selectors, and `[anthropic]`, `[openai]` and
`[google]` add a model provider.

The store app is opt-in. To persist conversations, attachments, steps and memory
in the database, add it to `INSTALLED_APPS`, where its label is
`django_spec_agents_store`, and run `migrate`:

```python
INSTALLED_APPS = [
    # ...
    "django_spec_agents.contrib.store",
]
```

## Quickstart

An agent over tools you register:

```python
from django_spec_agents import AgentConfig, AgentDeps, ToolRegistry, build_agent, tool

registry = ToolRegistry()


@tool(registry)
def list_orders(status: str) -> list[dict]:
    """List orders with the given status."""
    ...


agent = build_agent(registry, AgentConfig(model="anthropic:claude-sonnet-4-5"))
result = await agent.run("what is still pending?", deps=AgentDeps(user=request.user))
```

The same agent calling your specs beside the registry's tools, with `[specs]`
installed. Each key is a tool name, distinct from the registry's; the description, the parameter schema and the return schema come from the
spec. Each spec declares its `permission_classes`, which the toolset refuses to
go without, because nothing over HTTP gates a call made in process:

```python
from django_spec_agents.spec_tools import SpecToolset

toolset = SpecToolset({"list_invoices": list_invoices_spec, "create_invoice": create_invoice_spec})
agent = build_agent(registry, AgentConfig(model="anthropic:claude-sonnet-4-5", toolsets=[toolset]))
```

## Documentation

<https://artui.github.io/django-spec-agents/>

- [Concepts](https://artui.github.io/django-spec-agents/concepts/): how a config
  becomes an agent, and what belongs here versus in a transport.
- [Tools](https://artui.github.io/django-spec-agents/tools/): registering tools
  and what their metadata does.
- [Storage](https://artui.github.io/django-spec-agents/storage/): the store
  contracts, owner scoping and the store app.
- [Policy](https://artui.github.io/django-spec-agents/policy/): audit, the
  approval gate and the failure policy.
- [Integrations](https://artui.github.io/django-spec-agents/integrations/): specs
  and MCP tools as agent tools.
- [Spec tools](https://artui.github.io/django-spec-agents/spec-tools/):
  `SpecToolset` and `SpecCapability` in depth.
- [Reference](https://artui.github.io/django-spec-agents/reference/): the public
  API.

## License

MIT. See [LICENSE](https://github.com/Artui/django-spec-agents/blob/main/LICENSE).
