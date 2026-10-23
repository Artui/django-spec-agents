# django-spec-agents

[Pydantic-AI](https://ai.pydantic.dev) agents for Django, in two halves under one
import path, `django_spec_agents`:

- **The agent host** takes an `AgentConfig` and a tool registry and returns a
  built `pydantic_ai.Agent`, with composed toolsets, audit, an approval gate, a
  failure policy and user resolution, and reads **no** Django settings of its own.
  Each transport ([`django-ag-ui`](https://github.com/Artui/django-ag-ui) for
  AG-UI over SSE, `django-a2a` for agent-to-agent) owns its settings namespace,
  builds an `AgentConfig`, and hands it down.
- **[`spec_tools`](spec-tools/index.md)**, behind the `[specs]` extra, serves
  `django-service-specs` specs to an agent as tools, in process, with each
  spec's own validation, permissions and rendering.

```bash
pip install django-spec-agents            # the agent host
pip install "django-spec-agents[specs]"   # and specs as agent tools
```

The host was published as `django-pydantic-agent` and `spec_tools` as
`djangorestframework-pydantic-ai`. The package root never imports `spec_tools`, so
the host installs and runs without the extra.

## Is this the package you want?

Often not directly: **most projects install a transport** and get this as a
dependency. Reach for it on its own when you are:

- **building a transport**, a new wire protocol over the same agent host;
- **sharing agent construction** between transports, so one project's AG-UI
  endpoint and its A2A endpoint expose the same tools, audit trail and policy;
- **embedding an agent** with no HTTP surface at all, a management command or a
  background task, where you want the registry, audit and guard machinery
  without a view;
- **giving an agent your specs as tools**, the same services and selectors your
  API serves, without standing up an MCP server.

If you want an agent in a browser, start with
[`django-ag-ui`](https://github.com/Artui/django-ag-ui).

## The agent host

- **[Agent construction](concepts.md)**: `AgentConfig` → `build_agent`, model
  and provider resolution, and an escape hatch to replace construction entirely.
- **[A tool registry](tools.md)**: `@tool` with `destructive` / `category` /
  `confirm` / `summary` metadata, and JSON Schema derived from the signature.
- **[Storage contracts](storage.md)**: `ConversationStore` / `AttachmentStore`
  protocols, reference implementations, and the opt-in store app,
  `django_spec_agents.contrib.store`, whose label is `django_spec_agents_store`.
- **[Policy](policy.md)**: an audit trail over every tool call, a server-side
  approval gate for destructive tools, and the failure policy.
- **[Integrations](integrations.md)**: the `[specs]` and `[drf-mcp]` bridges
  that compose specs or a `drf-mcp-server` registry into an agent the host builds.

## Specs as tools

`SpecToolset` turns a `name -> spec` mapping, or a spec registry, into
Pydantic-AI tools, and `SpecCapability` wraps it as a capability.
`django_spec_agents.spec_tools` exports those two and `AgentConventions`, and
nothing else; the deps a run passes are the host's
[`AgentDeps`](reference/agent.md#django_spec_agents.AgentDeps).

- **[Specs as tools](spec-tools/index.md)**: what the toolset routes through,
  and how it compares with an MCP server.
- **[Quickstart](spec-tools/quickstart.md)**: specs, a toolset, an agent run.
- **[Reference](spec-tools/reference.md)**: `SpecToolset`, `SpecCapability` and
  `AgentConventions`.

## The rule that shapes everything

**The package reads no Django settings of its own.** There is no `DJANGO_*`
lookup anywhere in it. Its public input is an `AgentConfig` dataclass; each
transport owns its own settings namespace, resolves its config, and hands the
record down. What it does read is Django's and DRF's own configuration:
`STORAGES`, for the store app's optional attachment storage alias, and DRF's
settings, where `spec_tools` and the `[drf-mcp]` bridge run through DRF.

That is what keeps transports independent: `django-ag-ui` reads `DJANGO_AG_UI`,
`django-a2a` reads its own, and neither fights the other for keys. A settings
read added here would turn every consumer's upgrade into a key migration. It is
also why collaborators are always passed as live objects rather than dotted
paths: there is no `import_string` in this package.

## Where next

- **[Concepts](concepts.md)**: how a config becomes an agent, and what belongs
  here versus in a transport.
- **[Tools](tools.md)**: registering tools and what the metadata does.
- **[Storage](storage.md)**: the store contracts and the store app.
- **[Policy](policy.md)**: audit, the destructive-tool gate and the failure
  policy.
- **[Integrations](integrations.md)**: specs and MCP tools as agent tools.
- **[Spec tools](spec-tools/index.md)**: `SpecToolset` and `SpecCapability` in
  depth.
- **[Reference](reference/index.md)**: the host's public API.
