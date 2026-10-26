"""What one tool call is dispatched with beyond its arguments: its seeds and its context.

The run's context is ``AgentDeps.call_context``, copied per call with the
tool's name under ``action``; the call's own seeds are what ``call_seeds``
returns, over the mount's ``pool_seeds``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import RequestFactory
from django_service_specs import (
    DEFAULT_POOL_SEEDS,
    Affordance,
    OutputField,
    Parameter,
    Parameters,
    PresentationContext,
    SelectorKind,
    SelectorSpec,
    ServiceSpec,
)
from pydantic_ai import RunContext

from django_spec_agents import AgentDeps
from django_spec_agents.spec_tools import SpecToolset
from tests.spec_tools.kernel_specs import RowPresenter, allow
from tests.spec_tools.test_spec_toolset import ctx_for


def _service(service: Callable[..., Any], **kwargs: Any) -> ServiceSpec:
    return ServiceSpec(
        service=service,
        atomic=False,
        permissions=allow(),
        **kwargs,
    )


async def _call_each(toolset: SpecToolset, ctx: Any, *names: str) -> list[Any]:
    tools = await toolset.get_tools(ctx)
    return [await toolset.call_tool(name, {}, ctx, tools[name]) for name in names]


# --- the run's context ---------------------------------------------------------


async def test_each_call_of_a_run_is_handed_its_own_copy_of_the_run_s_context():
    """The tool's name is the call's ``action``, and a stage's write stays in its call.

    Two calls of one run may run at once, so one dict shared by both could not
    say which tool each is. Each is handed a copy of the run's: the run's
    entries are the same objects in both, the name is each call's own, and
    what the first call's service wrote is absent from the second's and from
    the run's.
    """
    seen: list[dict[str, Any]] = []

    def record(call_context: dict[str, Any]) -> dict[str, bool]:
        """Record what the call carries."""
        seen.append(call_context)
        if call_context.get("action") == "first":
            call_context["written"] = True
        return {"ok": True}

    shared = object()
    deps = AgentDeps(user=User(username="u"))
    deps.call_context = {"shared": shared}
    toolset = SpecToolset({"first": _service(record), "second": _service(record)})

    await _call_each(toolset, ctx_for(deps.user, deps=deps), "first", "second")

    assert [context.get("action") for context in seen] == ["first", "second"]
    assert all(context.get("shared") is shared for context in seen)
    assert "written" in seen[0]
    assert "written" not in seen[1]
    assert deps.call_context == {"shared": shared}


async def test_the_three_standard_entries_are_none_where_the_run_put_none():
    """``request``, ``view`` and ``format`` are present on every call, as on HTTP."""
    seen: list[dict[str, Any]] = []

    def record(call_context: dict[str, Any]) -> dict[str, bool]:
        """Record what the call carries."""
        seen.append(dict(call_context))
        return {"ok": True}

    user = User(username="u")
    toolset = SpecToolset({"do_it": _service(record)})

    await _call_each(toolset, ctx_for(user), "do_it")

    assert seen == [
        {"request": None, "view": None, "format": None, "action": "do_it", "transport": "agent"}
    ]


async def test_every_call_says_it_came_through_an_agent_whatever_the_run_put_there():
    """``transport`` is ``"agent"`` on every call, the run's own entry of that
    name replaced as its ``action`` is, so a provider, a selector or a presenter
    reading ``call_context`` can tell an agent's call from one over HTTP when the
    run carries the request it arrived on."""
    seen: list[Any] = []

    def record(call_context: dict[str, Any]) -> dict[str, bool]:
        """Record the transport the call carries."""
        seen.append(call_context["transport"])
        return {"ok": True}

    deps = AgentDeps(user=User(username="u"))
    deps.call_context = {"request": RequestFactory().get("/"), "transport": "http"}
    toolset = SpecToolset({"do_it": _service(record)})

    await _call_each(toolset, ctx_for(deps.user, deps=deps), "do_it")

    assert seen == ["agent"]
    assert deps.call_context["transport"] == "http"


async def test_a_deps_type_without_the_field_gets_a_context_per_call():
    """A project's own deps type need not declare ``call_context``."""
    seen: list[dict[str, Any]] = []

    def record(call_context: dict[str, Any]) -> dict[str, bool]:
        """Record what the call carries."""
        seen.append(call_context)
        return {"ok": True}

    user = User(username="u")
    toolset = SpecToolset({"first": _service(record), "second": _service(record)})

    await _call_each(toolset, ctx_for(user, deps=SimpleNamespace(user=user)), "first", "second")

    assert [context.get("action") for context in seen] == ["first", "second"]
    assert seen[0] is not seen[1]


async def test_the_request_a_run_arrived_on_is_the_seed_a_callable_declares():
    """The run's ``request`` entry is the ``request`` a spec callable is handed.

    The kernel's pool has no ``request``; a spec callable written for HTTP
    declares one, and off HTTP it is the request the run arrived on, which
    whoever starts the run puts in the run's context.
    """
    arrived = RequestFactory().get("/agent/")

    def echo(request: Any) -> dict[str, bool]:
        """Report whether the request is the run's."""
        return {"same": request is arrived}

    user = User(username="u")
    deps = AgentDeps(user=user)
    deps.call_context = {"request": arrived}
    toolset = SpecToolset({"echo": _service(echo)})

    assert await _call_each(toolset, ctx_for(user, deps=deps), "echo") == [{"same": True}]


# --- the call's own seeds ------------------------------------------------------


class _Tenanted(SpecToolset):
    """Seeds each call with a tenant built from the run and the tool's name."""

    def call_seeds(
        self, *, ctx: RunContext[Any], tool_name: str
    ) -> Mapping[str, Callable[..., Any]]:
        username = ctx.deps.user.username
        return {"tenant": lambda: f"{username}:{tool_name}"}


async def test_a_seed_the_hook_returns_reaches_the_call_and_is_not_asked_of_the_model():
    """The hook's seed fills a parameter with no default, so the model is never asked for it.

    The required-arguments check reads the call's seeds, the hook's among
    them; a hook answered after it would leave ``tenant`` refused as missing.
    """

    def where(tenant: str) -> dict[str, str]:
        """Say which tenant the call ran under."""
        return {"tenant": tenant}

    user = User(username="u")
    toolset = _Tenanted({"first": _service(where), "second": _service(where)})

    results = await _call_each(toolset, ctx_for(user), "first", "second")

    assert results == [{"tenant": "u:first"}, {"tenant": "u:second"}]


async def test_a_hook_seed_the_mount_already_registers_is_refused():
    """``PoolSeeds.extend`` refuses a name registered twice, rather than picking a winner."""

    def where(tenant: str) -> dict[str, str]:
        """Say which tenant the call ran under."""
        return {"tenant": tenant}

    user = User(username="u")
    toolset = _Tenanted(
        {"where": _service(where)},
        pool_seeds=DEFAULT_POOL_SEEDS.extend(tenant=lambda: "mount"),
    )

    with pytest.raises(ValueError, match="'tenant' is already registered"):
        await _call_each(toolset, ctx_for(user), "where")


def _approve(**_: Any) -> dict[str, bool]:
    """Approve the pending invoices."""
    return {"approved": True}


def _conditioned(when: Callable[..., Any]) -> ServiceSpec:
    return _service(
        _approve,
        affordances=[Affordance(code="closed", reason="Closed here.", when=when)],
    )


async def test_a_listing_asks_each_condition_with_its_own_tool_s_seeds():
    """The listing calls the hook per conditioned tool, as each call would."""

    class Gated(SpecToolset):
        def call_seeds(
            self, *, ctx: RunContext[Any], tool_name: str
        ) -> Mapping[str, Callable[..., Any]]:
            return {"open_here": lambda: tool_name == "approve"}

    toolset = Gated(
        {
            "approve": _conditioned(lambda open_here: open_here),
            "reject": _conditioned(lambda open_here: open_here),
        }
    )

    tools = await toolset.get_tools(ctx_for(User(username="u")))

    assert sorted(tools) == ["approve"]


async def test_a_kwargs_provider_reads_the_transport_off_the_call_s_context():
    """A provider that scopes differently for an agent's call declares
    ``call_context`` and finds ``transport`` there, as a selector does."""
    seen: list[Any] = []

    def scope(call_context: dict[str, Any]) -> dict[str, Any]:
        seen.append(call_context["transport"])
        return {}

    toolset = SpecToolset({"projects": _selecting_spec(kwargs=scope)})

    await _call_query(toolset, {"query": "name"})

    assert seen == ["agent"]


async def test_a_listing_hands_each_condition_the_transport_a_call_would():
    """A condition is asked with the context its call would carry, ``transport``
    included, so a tool gated on being reached by an agent is listed."""
    toolset = SpecToolset(
        {"approve": _conditioned(lambda call_context: call_context["transport"] == "agent")}
    )

    tools = await toolset.get_tools(ctx_for(User(username="u")))

    assert sorted(tools) == ["approve"]


async def test_a_listing_hands_each_condition_its_own_tool_s_name_as_the_action():
    """A condition naming ``call_context`` reads the operation it is asked about."""
    toolset = SpecToolset(
        {
            "approve": _conditioned(lambda call_context: call_context["action"] == "approve"),
            "reject": _conditioned(lambda call_context: call_context["action"] == "approve"),
        }
    )

    tools = await toolset.get_tools(ctx_for(User(username="u")))

    assert sorted(tools) == ["approve"]


# --- one read the selector takes and the presenter sees --------------------------


class _Selecting(RowPresenter):
    """Renders the fields the call's ``query`` names, read off the call's context."""

    def presented(self, value: Any, context: PresentationContext | None) -> Any:
        assert context is not None
        assert context.call_context["transport"] == "agent"
        wanted = context.call_context["query"].split(",")
        return {name: rendered for name, rendered in self.present(value).items() if name in wanted}


def _projects(query: str, call_context: dict[str, Any]) -> list[dict[str, str]]:
    """List the projects, eager-loading what ``query`` asks for."""
    call_context["query"] = query
    return [{"name": "Atlas", "code": "AT"}, {"name": "Borealis", "code": "BO"}]


def _selecting_spec(**kwargs: Any) -> SelectorSpec:
    return SelectorSpec(
        kind=SelectorKind.LIST,
        selector=_projects,
        reads=Parameters.of(Parameter("query", "string", required=True)),
        presenter=_Selecting(OutputField("name", "string"), OutputField("code", "string")),
        permissions=allow(),
        **kwargs,
    )


async def _call_query(toolset: SpecToolset, args: dict[str, Any]) -> Any:
    ctx = ctx_for(User(username="u"))
    tools = await toolset.get_tools(ctx)
    return await toolset.call_tool("projects", args, ctx, tools["projects"])


async def test_a_read_the_selector_hands_on_reaches_the_presenter_of_a_page():
    """The quickstart's recipe for one read both halves need: the selector takes
    ``query`` as a read and writes it to the call's context, and the presenter
    reads it there, on the page the toolset presents, which is handed the
    dispatch's own context rather than a fresh one."""
    toolset = SpecToolset({"projects": _selecting_spec()})

    result = await _call_query(toolset, {"query": "name", "limit": 1})

    assert result["items"] == [{"name": "Atlas"}]
    assert result["hasNext"] is True


# --- a callable that sees the whole page, and the shapes of the call --------------


class _Recording(RowPresenter):
    """Records the rows of each ``presented_rows`` call, then presents them as usual."""

    def __init__(self, *fields: OutputField) -> None:
        super().__init__(*fields)
        self.pages: list[list[Any]] = []

    def presented_rows(self, values: Sequence[Any], context: PresentationContext | None) -> Any:
        self.pages.append(list(values))
        return super().presented_rows(values, context)


async def test_a_presenter_is_asked_for_the_rows_of_a_page_once_per_page_served():
    """The toolset slices a list before it renders, so the kernel's one
    ``presented_rows`` call per list result is one call per page, holding
    exactly that page's rows and not the selector's whole answer."""
    presenter = _Recording(OutputField("name", "string"), OutputField("code", "string"))
    spec = SelectorSpec(
        kind=SelectorKind.LIST,
        selector=_projects,
        reads=Parameters.of(Parameter("query", "string", required=True)),
        presenter=presenter,
        permissions=allow(),
    )
    toolset = SpecToolset({"projects": spec})
    atlas = {"name": "Atlas", "code": "AT"}
    borealis = {"name": "Borealis", "code": "BO"}

    await _call_query(toolset, {"query": "name", "limit": 1})
    assert presenter.pages == [[atlas]]
    await _call_query(toolset, {"query": "name", "limit": 1, "page": 2})

    assert presenter.pages == [[atlas], [borealis]]


async def test_a_selector_declaring_shapes_is_handed_the_shape_values_the_agent_sent():
    """The kernel's reserved ``shapes`` seed reaches a callable through this
    transport: the selector reads the call's shape values by declaring
    ``shapes``, and the model is never asked for it."""
    seen: list[Mapping[str, Any]] = []

    def projects(shapes: Mapping[str, Any]) -> list[dict[str, str]]:
        """List the projects."""
        seen.append(shapes)
        return [{"name": "Atlas"}]

    spec = SelectorSpec(
        kind=SelectorKind.LIST,
        selector=projects,
        shapes=Parameters.of(Parameter("fields", "string", help="The fields to return.")),
        presenter=RowPresenter(OutputField("name", "string")),
        permissions=allow(),
    )
    toolset = SpecToolset({"projects": spec})
    ctx = ctx_for(User(username="u"))
    tools = await toolset.get_tools(ctx)

    await toolset.call_tool("projects", {"fields": "name"}, ctx, tools["projects"])
    await toolset.call_tool("projects", {}, ctx, tools["projects"])

    assert [dict(shapes) for shapes in seen] == [{"fields": "name"}, {}]
    # A shape the model leaves out, and declares no default for, is absent
    # rather than ``None``, so a callable reads it with ``get``.
    # The mapping is read-only: a callable cannot write through it.
    writable: Any = seen[0]
    with pytest.raises(TypeError):
        writable["fields"] = "code"
    assert set(tools["projects"].tool_def.parameters_json_schema["properties"]) == {
        "fields",
        "page",
        "limit",
    }


async def test_a_selector_declaring_shapes_on_a_spec_with_none_is_handed_an_empty_mapping():
    """The quickstart's claim that ``shapes`` is empty where the spec declares
    no shape: the seed is always there to be read."""
    seen: list[dict[str, Any]] = []

    def projects(shapes: Mapping[str, Any]) -> list[dict[str, str]]:
        """List the projects."""
        seen.append(dict(shapes))
        return [{"name": "Atlas"}]

    spec = SelectorSpec(
        kind=SelectorKind.LIST,
        selector=projects,
        presenter=RowPresenter(OutputField("name", "string")),
        permissions=allow(),
    )
    toolset = SpecToolset({"projects": spec})
    ctx = ctx_for(User(username="u"))
    tools = await toolset.get_tools(ctx)

    await toolset.call_tool("projects", {}, ctx, tools["projects"])

    assert seen == [{}]
