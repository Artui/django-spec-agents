# Moving from the old packages

`django-spec-agents` is two packages merged under one import path. The agent
host was published as `django-pydantic-agent`, and `django_spec_agents.spec_tools`
as `djangorestframework-pydantic-ai`. Both have had their final release, and both
keep working exactly as that release did: nothing in them warns, breaks or
re-exports. This page is for the day you move.

A project that used both moves both halves at once. Read the two sections
below, then [the seams 0.2.0 changes](#the-seams-020-changes), which apply to
anyone serving specs as tools.

## From django-pydantic-agent

### Install

```bash
pip install django-spec-agents
```

`anthropic`, `openai`, `google`, `drf-mcp` and `harness` keep their names. The
`[spec-tools]` extra, which pulled `djangorestframework-pydantic-ai`, becomes
`django-spec-agents[specs]`.

### Imports

The package's layout carries over under the new root:

| Was | Now |
| --- | --- |
| `django_pydantic_agent` | `django_spec_agents`, with the same root names |
| `django_pydantic_agent.<module>` | `django_spec_agents.<module>`, such as `django_spec_agents.contrib.store` |
| `django_pydantic_agent.constants` | one module per name under `django_spec_agents.types` |

`ToolCategory`, `X_DESTRUCTIVE_KEY`, `X_CATEGORY_KEY`, `X_CONFIRM_KEY`,
`X_SUMMARY_KEY` and `DESTRUCTIVE_METADATA_KEY` keep their spelling and are
imported from the root:

```python
from django_spec_agents import DESTRUCTIVE_METADATA_KEY, ToolCategory, X_CONFIRM_KEY
```

`ATTACHMENT_STORAGE_ALIAS` is not a root name; import it from
`django_spec_agents.types.attachment_storage_alias`.

### `INSTALLED_APPS` and the store's tables

1. Run `migrate` on django-pydantic-agent's final release first. The new store
   renames the old store's tables in place, and needs every one of the old
   store's migrations applied before it runs; short of them it stops with
   `OldInstallBehindError` and renames nothing.
2. Replace the dependency, and change every `django_pydantic_agent` import to
   `django_spec_agents`.
3. Change the `INSTALLED_APPS` entry:

    ```python
    INSTALLED_APPS = [
        ...,
        "django_spec_agents.contrib.store",  # was "django_pydantic_agent.contrib.store"
    ]
    ```

4. Run `migrate` once. The app's label is `django_spec_agents_store`, and its one
   migration, `0001_initial`, renames the tables, their named constraints and
   indexes, the content types and the migration records. Nothing is copied.

What the rename touches, what it leaves under the old names on PostgreSQL and
how to roll it back are on the [storage page](storage.md#the-app-label-and-upgrading-from-django-pydantic-agent).

**A project migration that names the old label** stops loading once the old app
leaves `INSTALLED_APPS`: a dependency such as
`("django_pydantic_agent_store", "0005_storedmemory_storedmemoryoperation")`
raises `NodeNotFoundError`, and a foreign key to
`"django_pydantic_agent_store.StoredConversation"` fails the system check.
Point the foreign key, in the model and in the migration, at
`django_spec_agents_store.<Model>`, and the dependency at
`("django_spec_agents_store", "0001_initial")`. Your migration is recorded as
applied and now depends on one that is not, which Django refuses as
`InconsistentMigrationHistory`, so run that first `migrate` with the apps holding
such migrations left out of `INSTALLED_APPS`, then put them back. The rows your
foreign keys point at are kept.

### Names that keep their spelling

Each is stored or configured outside Python, so a rename would break a project
silently. They keep django-pydantic-agent's spelling:

- the attachment storage alias `django_pydantic_agent_attachments`;
- the session key `django_pydantic_agent_conversations`;
- the attachment upload prefix `django_pydantic_agent/attachments/`;
- the `django_pydantic_agent.audit` and `django_pydantic_agent.failure` loggers;
- the `agent_store_*` management commands.

### Names that change

- `DESTRUCTIVE_METADATA_KEY` is `"django_spec_agents.destructive"`.
- The attachments toolset id is `django-spec-agents-attachments`.
- The audit `ContextVar` is named `django_spec_agents.audit.execution`.
- Model-build and failure-policy messages start `django-spec-agents: `.
- A registry tool registered under its own name, with `@tool(registry, name="...")`
  or as a `ToolSpec` registered directly, is offered to the model under that
  name, where django-pydantic-agent offered the function's. A `require_approval`
  or `exempt` entry, an approval prompt, or a query on the audit or step store's
  `tool_name` that named such a tool by its function name uses the registered
  name after moving. A `require_approval` entry left on the function name no
  longer matches, so that tool runs without approval.

## From djangorestframework-pydantic-ai

### Install

```bash
pip install "django-spec-agents[specs]"
```

`[specs]` installs the spec kernel, django-service-specs, and nothing else.
There is no `[filter]` extra: a project declaring a `filter_set` has
django-filter installed already, and `django-service-specs[filter]` declares it.
A project reaching the toolset through django-ag-ui installs `django-ag-ui[specs]`
where it installed `[spec-tools]`.

### Where each name went

| Was | Now |
| --- | --- |
| `rest_framework_pydantic_ai.SpecToolset` | `django_spec_agents.spec_tools.SpecToolset` |
| `rest_framework_pydantic_ai.SpecCapability` | `django_spec_agents.spec_tools.SpecCapability` |
| `rest_framework_pydantic_ai.AgentConventions` | `django_spec_agents.spec_tools.AgentConventions` |
| `rest_framework_pydantic_ai.AgentDeps` | `django_spec_agents.AgentDeps`, the host's own deps type |
| `rest_framework_pydantic_ai.QueryParam`, `UrlKwarg` | a kernel `Parameter` in the spec's `reads=` or `shapes=`; see [Specs](#specs) |
| `rest_framework_pydantic_ai.testing` | `django_spec_agents.testing`, with the same three doubles |
| `rest_framework_pydantic_ai.__version__` | `django_spec_agents.__version__` |
| `rest_framework_pydantic_ai.spec_toolset`, `.spec_capability`, `.types.agent_conventions` | the same module under `django_spec_agents.spec_tools`, such as `django_spec_agents.spec_tools.spec_toolset` for the `SpecSource` alias a subclass annotates with |

```python
from django_spec_agents import AgentDeps
from django_spec_agents.spec_tools import AgentConventions, SpecCapability, SpecToolset
```

**The logger** is `django_spec_agents.spec_tools`. A `LOGGING` entry under
`rest_framework_pydantic_ai` stops receiving the toolset's records, and nothing
fails to tell you.

**The host's `AgentDeps` has more fields.** It is `user, ip_address, state,
progress, call_context`, so a positional `AgentDeps(user, reporter)` binds the
reporter to `ip_address` without complaint. Pass `progress=` by keyword.

### Specs

The toolset serves the kernel's specs, `ServiceSpec` and `SelectorSpec` from
`django_service_specs`, in a mapping or a `SpecRegistry`. A
djangorestframework-services spec is not a supported source: declare each spec
the agent serves on the kernel. What that changes for a tool:

- **A read is a `Parameter`.** What the model sends for the callable goes in
  `reads=`, and what shapes the output and reaches only the presenter in
  `shapes=`. A selector or provider takes a read by its name, as an argument.
- **Audiences are markings on the output.** What `agent_contract` declared moves
  onto the spec: its reads to `reads=` and `shapes=`, its field audiences to a
  `FieldMarking` on each field of the presenter's output, as
  [hiding plumbing from the model](spec-tools/agent-audience.md) shows.
- **Permissions are the kernel's `PermissionCheck` instances**, in
  `permissions=`. A spec declaring none is refused when the toolset is built;
  `require_permissions=False` downgrades that to a warning while migrating, and
  `permissions=[Unrestricted()]` says anyone may call it.
- **A filter's `ordering` is an array** of sort names. A string is handed back
  to the model as `ordering: Expected an array.`, so an agent prompt or a tool
  description that describes it as one comma-separated value says the wrong
  thing.

```python
from django_service_specs import (
    DataclassPresenter,
    Parameter,
    Parameters,
    SelectorKind,
    SelectorSpec,
    Unrestricted,
)

list_orders_spec = SelectorSpec(
    kind=SelectorKind.LIST,
    selector=list_orders,  # def list_orders(*, user, status=None)
    reads=Parameters.of(Parameter("status", "string")),
    presenter=DataclassPresenter(OrderOut),
    permissions=[Unrestricted()],
)
```

### Mount keywords

`query_params=`, `tool_query_params=`, `url_kwargs=` and `tool_url_kwargs=` are
gone, and passing one raises `TypeError`. A read the model sends is declared on
the spec, in `reads=`. A value the server supplies, such as a capture bound to
the acting user's project, goes in `tool_server_arguments=`, one kernel
`ServerArguments` per tool, built per run where the value depends on the user:

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

A bound name is filled whatever the model sends, a withheld one is left to its
default, and neither is advertised. See
[arguments the server supplies](spec-tools/spec-registry.md#arguments-the-server-supplies).

## The seams 0.2.0 changes

These changed between django-spec-agents 0.1.0 and 0.2.0, so they apply to a
project moving from either old package and to one already on 0.1.0.

### What a call carries besides its arguments

The toolset builds no request and no view for a call. What each part of a spec
sees:

- **A permission check** sees the call's arguments. A kernel `PermissionCheck`
  method declaring the keyword `arguments` is handed them, one declaring
  `call_context` the call's context, with the tool's name under `action`, and
  one named after a registered seed that seed's value. See
  [what a permission check sees](spec-tools/quickstart.md#what-a-permission-check-sees).
- **A provider or selector declaring `request` or `view` receives `None`**
  unless the run puts one in `call_context` or a `call_seeds` override returns
  one. A value it used to read off `view.kwargs` or `request.query_params` it
  now takes by name, as a read.
- **A callable that told the transport by `isinstance(view, OfflineServiceView)`**
  declares `call_context` and reads `call_context["transport"]`, which is
  `"agent"` on every call the toolset makes. The request the run arrived on may
  be in `call_context` too, so its presence says nothing about how the call
  came.

The request a run arrived on goes in the run's context, where the toolset's
`http_request=` and `get_http_request=` used to name it. A presenter can read it
there to make a URL absolute, for example with `request.build_absolute_uri`, where
`host=` named one; the kernel's presenters do not do this:

```python
from django_spec_agents import AgentDeps

deps = AgentDeps(user=request.user)
deps.call_context["request"] = request
result = await agent.run(prompt, deps=deps)
```

### `build_context` becomes `call_seeds`

An override of `build_context` that varied the context per run becomes an
override of `call_seeds(*, ctx, tool_name)`, returning the call's own seeds,
name to resolver. A `view` or `request` it returns replaces the call's own, a
name the mount's `pool_seeds` registers too is refused with `ValueError`, and a
seed for a read the spec declares is refused with `ImproperlyConfigured`:

```python
from django_spec_agents.spec_tools import SpecToolset


class TenantToolset(SpecToolset):
    def call_seeds(self, *, ctx, tool_name):
        tenant = ctx.deps.tenant
        return {"tenant": lambda: tenant}
```

See [per-call seeds and the run's context](spec-tools/quickstart.md#per-call-seeds-and-the-runs-context).

### Rendering and paging

- `render_output(spec, result, *, ctx, projection)` takes the kernel's
  `DispatchResult` and defaults to the kernel's `present_for_audience`. The
  `many`, `request`, `view` and `extras` arguments are gone; an override that
  wants the unprojected payload calls the kernel's `present(spec, result)`.
- `output_extras` is gone, and with it the `page`, `result` and `instance` keys
  an output-context provider was handed. The kernel presents a result row by
  row, so a provider that batched lookups over the rendered page has no page to
  batch over.
- `shape_page(rows, *, ctx, page, limit, max_page_size)` returns the kernel's
  `OutputPage`, from `django_service_specs`. A subclass of another library's page
  type subclasses that one.
- `is_tool_listed(name, ctx)` is unchanged: the seam for a per-run catalog,
  such as hiding a staff-only tool from a non-staff run. Hiding a tool grants
  nothing: the spec's permissions are asked on every call, whatever it
  returns.

### Other changes

- `json_schema_registry=` is gone. A field's schema is whatever the spec
  compiles for it; describe it on the spec.
- The default `id` of `SpecToolset` and `SpecCapability` is `spec-tools`, where it
  was `drf-specs`. Pass `id="drf-specs"` to keep anything keyed on it.
- A call is answered by the kernel's refusals only. Django REST Framework's
  `PermissionDenied` and `ValidationError` raised inside a callable escape the
  call unchanged; raise the kernel's `NotPermitted` and `ServiceValidationError`
  instead.

The full list is the [changelog](https://github.com/Artui/django-spec-agents/blob/main/CHANGELOG.md).
