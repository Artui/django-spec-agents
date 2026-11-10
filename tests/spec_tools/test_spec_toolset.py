from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import threading
import time
import warnings
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, is_dataclass, replace
from dataclasses import fields as dataclass_fields
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Generic, TypeVar

import django_filters
import django_service_specs
import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django import forms
from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import FieldError, ImproperlyConfigured
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection, connections
from django.db.models import Q
from django.http import Http404
from django.test import RequestFactory
from django_service_specs import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_POOL_SEEDS,
    UNSET,
    ActionUnavailable,
    AdditionalInputRequired,
    Affordance,
    FieldMarking,
    FormValidator,
    InvalidArguments,
    NotPermitted,
    Output,
    OutputField,
    Parameter,
    Parameters,
    PermissionCheck,
    PoolSeeds,
    Presenter,
    PrincipalUnavailable,
    SelectorKind,
    SelectorSpec,
    ServerArguments,
    ServiceConflict,
    ServiceError,
    ServiceSpec,
    ServiceValidationError,
    SpecRegistry,
    UnknownArguments,
    Unrestricted,
    UnsetType,
    audience_projection_for_spec,
    present,
    provider_keys,
    spec_input_schema,
    spec_output_schema,
)
from pydantic_ai import Agent, ModelRetry, RunContext, ToolFailed
from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from typing_extensions import TypedDict, Unpack

from django_spec_agents import AgentDeps
from django_spec_agents.spec_tools import AgentConventions, SpecToolset, spec_toolset
from django_spec_agents.spec_tools.spec_toolset import (
    UndescribedToolWarning,
    UnguardedSpecWarning,
    _build_tool_def,
    _default_get_progress,
    _derive_instructions,
    _fill_reads,
    _input_schema,
    _is_list_selector,
    _return_schema,
    _shape_list,
    _spec_ordering_argument,
    _with_deadline,
)
from django_spec_agents.testing import (
    instruction_capturing_model,
    tool_calling_model,
)
from tests.spec_tools.kernel_specs import (
    DenyAll,
    RowPresenter,
    allow,
    shaping,
    widget_input,
    widget_presenter,
)
from tests.spec_tools.testapp.models import Widget
from tests.tool_retries import is_tool_answer, is_tool_retry, tool_answers, tool_retries

if TYPE_CHECKING:
    # Annotation-only imports, where a project running ruff's flake8-type-checking
    # rules moves them. A ``kwargs=`` provider annotated with one has to keep its
    # keys readable: see the provider tests, which name what each one holds.
    from django.contrib.auth.models import Group
    from django.http import HttpRequest

# Written out rather than imported: they are the text the model reads, and a
# test comparing the module's constant to itself would pass whatever it said.
_SCOPE_DESCRIPTION = (
    "On a paged result it applies to each item in `items`, never to the page envelope "
    "(`items`, `page`, `totalPages`, `hasNext`)."
)
_SCOPE_INSTRUCTION = (
    "On a tool that returns a page, they apply to each item in `items`, never to the page itself."
)


# The wording the toolset says by default, for the tests about *whether* a line
# is said. What each default says is pinned as text in ``test_default_wording.py``.
_DEFAULTS = AgentConventions()


def _pagination_line(page_size: int) -> str:
    envelope = '{"items": [...], "page": 1, "totalPages": N, "hasNext": true|false}'
    return _DEFAULTS.pagination.format(page_size=page_size, envelope=envelope)


def _ordering_line(names: list[str]) -> str:
    return _DEFAULTS.ordering.format(names=", ".join(f"`{name}`" for name in names))


# --- specs under test --------------------------------------------------------


def list_widgets(user):
    """List widgets owned by the acting user."""
    return Widget.objects.filter(owner=user)


def get_widget(user, pk):
    """Fetch a single widget by primary key."""
    return Widget.objects.filter(owner=user, pk=pk)


def create_widget(data, user):
    """Create a widget for the acting user."""
    return Widget.objects.create(owner=user, **data)


def boom():
    """Always fails with a business error."""
    raise ServiceError("nope")


def reject():
    """Always rejects its input."""
    raise ServiceValidationError("bad input")


def _dispatch(
    spec: Any,
    user: Any,
    args: dict[str, Any] | None = None,
    *,
    toolset_kwargs: dict[str, Any] | None = None,
    reads: Sequence[Parameter] = (),
    shapes: Sequence[Parameter] = (),
    **kwargs: Any,
) -> Any:
    """Run one spec through the toolset's dispatch pipeline."""
    toolset_kwargs = toolset_kwargs or {}
    if reads:
        spec = replace(spec, reads=Parameters.of(*spec.reads, *reads))
    if shapes:
        spec = replace(spec, shapes=Parameters.of(*spec.shapes, *shapes))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        toolset = SpecToolset({"t": spec}, require_permissions=False, **toolset_kwargs)
    ctx = ctx_for(user)
    kwargs.setdefault("label", "t")
    return toolset._call_spec(spec, user, dict(args or {}), ctx=ctx, **kwargs)


# The three builders are guarded by default: ``require_permissions`` defaults to
# True, so an unguarded spec is a construction error and every fixture would trip it.


def list_spec(**kwargs):
    kwargs.setdefault("permissions", allow())
    kwargs.setdefault("selector", list_widgets)
    return SelectorSpec(
        kind=SelectorKind.LIST,
        presenter=widget_presenter(),
        **kwargs,
    )


#: The read a lookup by primary key declares: the kernel hands a selector only
#: the arguments its spec declares.
_PK = Parameter("pk", "integer", required=True)


def by_pk(selector: Any, **kwargs: Any) -> SelectorSpec:
    """A ``RETRIEVE`` of one row by ``pk``: a write's target lookup, as nested."""
    return SelectorSpec(
        kind=SelectorKind.RETRIEVE, selector=selector, reads=Parameters.of(_PK), **kwargs
    )


def retrieve_spec(**kwargs):
    kwargs.setdefault("permissions", allow())
    kwargs.setdefault("selector", get_widget)
    kwargs.setdefault("reads", Parameters.of(_PK))
    kwargs.setdefault("presenter", widget_presenter())
    return SelectorSpec(kind=SelectorKind.RETRIEVE, **kwargs)


def create_spec(**kwargs):
    kwargs.setdefault("permissions", allow())
    return ServiceSpec(
        service=create_widget,
        validator=widget_input(),
        presenter=widget_presenter(),
        **kwargs,
    )


#: Every ``RunContext`` field this package reads. Kept as data because
#: ``ctx_for`` builds the double from it and
#: ``test_the_run_context_double_carries_every_field_the_package_reads`` pins it
#: against the real dataclass -- a double shaped by whatever the tests happened
#: to need is how a helper ends up describing a context no run produces.
RUN_CONTEXT_FIELDS_READ = frozenset(
    {"deps", "run_id", "conversation_id", "run_step", "tool_call_id", "usage"}
)


def ctx_for(user=None, *, deps=None, **overrides):
    """A stand-in for the live ``RunContext``, carrying every field we read."""
    fields = {
        "deps": deps if deps is not None else AgentDeps(user=user),
        "run_id": "run-1",
        "conversation_id": "conv-1",
        "run_step": 1,
        "tool_call_id": "call-1",
        "usage": RunUsage(),
    }
    return SimpleNamespace(**(fields | overrides))


def reading(specs, reads):
    """Each tool ``reads`` names declares those reads on its spec, after any it declares."""
    registry = SpecRegistry()
    for name, spec in specs.items():
        extra = tuple(reads.get(name, ()))
        registry.register(
            name, replace(spec, reads=Parameters.of(*spec.reads, *extra)) if extra else spec
        )
    return registry


def _rows(result: Any) -> list[Any]:
    """The rows out of a list tool's page envelope."""
    assert set(result) == {"items", "page", "totalPages", "hasNext"}
    return result["items"]


# --- get_instructions --------------------------------------------------------
#
# ``get_instructions`` derives the conventions block from the specs; it ignores
# ``ctx`` (no per-run state), so the tests pass ``None``.


async def test_instructions_carry_the_error_contract():
    instr = await SpecToolset({"go": create_spec()}).get_instructions(None)
    assert instr is not None
    assert _DEFAULTS.base in instr
    # Every terminal arm raises ``ToolFailed``; no ``{"error"}`` payload exists.
    assert '{"error"' not in instr
    assert "comes back as a failed call" in instr
    assert "unknown arguments are rejected" in instr
    assert "`(code: <name>)`" in instr
    assert "`affordances`" in instr
    assert "that is a final answer, not a reason to retry" in instr


async def test_pagination_line_present_only_with_a_list_selector():
    with_list = await SpecToolset({"list": list_spec()}).get_instructions(None)
    assert _pagination_line(DEFAULT_PAGE_SIZE) in with_list

    # A retrieve selector is a SelectorSpec but not LIST — no pagination line.
    retrieve_only = await SpecToolset({"get": retrieve_spec()}).get_instructions(None)
    assert _pagination_line(DEFAULT_PAGE_SIZE) not in retrieve_only

    service_only = await SpecToolset({"go": create_spec()}).get_instructions(None)
    assert _pagination_line(DEFAULT_PAGE_SIZE) not in service_only


@pytest.mark.parametrize("source", ["registry", "mapping"])
async def test_read_shaping_line_lists_declared_shapes_sorted(source):
    """The compiled spec's shapes, so a mapping-built tool advertises the same arguments."""
    specs = {
        "list": replace(
            list_spec(),
            shapes=Parameters.of(Parameter("query", "string"), Parameter("fields", "string")),
        )
    }
    toolset = SpecToolset(reading(specs, {}) if source == "registry" else specs)
    instr = await toolset.get_instructions(None)
    assert instr is not None
    assert "read-shaping parameters (`fields`, `query`)" in instr
    assert _SCOPE_INSTRUCTION in instr


async def test_read_shaping_line_absent_without_query_params():
    instr = await SpecToolset({"list": list_spec()}).get_instructions(None)
    assert "read-shaping parameters" not in instr


async def test_instructions_override_wins_verbatim():
    toolset = SpecToolset({"list": list_spec()}, instructions="just do it")
    assert await toolset.get_instructions(None) == "just do it"


def test_is_list_selector():
    assert _is_list_selector(list_spec()) is True
    assert _is_list_selector(retrieve_spec()) is False
    assert _is_list_selector(create_spec()) is False


def test_derive_instructions_matches_get_instructions_input():
    toolset = SpecToolset(shaping({"list": list_spec()}, {"list": [Parameter("query", "string")]}))
    # The public ``get_instructions`` derives from exactly these two mappings.
    shapes = {name: toolset._offered_shapes(name) for name in toolset._specs}
    assert _derive_instructions(toolset._specs, shapes) == "\n".join(
        [
            _DEFAULTS.base,
            _pagination_line(DEFAULT_PAGE_SIZE),
            "- Some tools accept read-shaping parameters (`query`) that adjust the shape "
            "of the returned data without filtering it. " + _SCOPE_INSTRUCTION,
        ]
    )


# --- get_tools ---------------------------------------------------------------


async def test_get_tools_builds_function_tools():
    toolset = SpecToolset({"list_widgets": list_spec(), "create_widget": create_spec()})
    tools = await toolset.get_tools(None)
    assert set(tools) == {"list_widgets", "create_widget"}
    # The in-process kind: the run loop invokes ``call_tool``, not a client-deferred call.
    assert all(tool.tool_def.kind == "function" for tool in tools.values())


async def test_get_tools_description_from_docstring():
    toolset = SpecToolset({"list_widgets": list_spec()})
    tools = await toolset.get_tools(None)
    assert tools["list_widgets"].tool_def.description == "List widgets owned by the acting user."


async def test_list_selector_tool_advertises_pagination_args():
    toolset = SpecToolset({"list_widgets": list_spec()})
    tools = await toolset.get_tools(None)
    props = tools["list_widgets"].tool_def.parameters_json_schema["properties"]
    assert {"page", "limit"} <= set(props)
    # ``ordering`` comes from the spec, never the toolset; this one declares none.
    assert "ordering" not in props


async def test_annotations_mark_read_vs_write():
    toolset = SpecToolset({"list_widgets": list_spec(), "create_widget": create_spec()})
    tools = await toolset.get_tools(None)
    assert tools["list_widgets"].tool_def.metadata == {"annotations": {"readOnlyHint": True}}
    assert tools["create_widget"].tool_def.metadata == {"annotations": {"readOnlyHint": False}}


def _undocumented_widgets(user):
    return Widget.objects.filter(owner=user)


def _undescribed_spec() -> SelectorSpec:
    """A spec nothing describes: its selector carries no docstring."""
    return SelectorSpec(
        kind=SelectorKind.LIST,
        selector=_undocumented_widgets,
        presenter=widget_presenter(),
        permissions=allow(),
    )


async def test_selector_without_docstring_has_no_description():
    with pytest.warns(UndescribedToolWarning):
        toolset = SpecToolset({"empty": _undescribed_spec()})
    tools = await toolset.get_tools(None)
    assert tools["empty"].tool_def.description is None


class _SelectorlessDeclaration:
    """A declaration of another library's whose ``as_kernel_spec`` refuses it."""

    def as_kernel_spec(self) -> SelectorSpec:
        raise ImproperlyConfigured("SelectorSpec declares no selector")


def test_a_selector_spec_with_no_selector_is_refused_when_the_toolset_is_built():
    """A library refuses a declaration it cannot compile, and the toolset compiles while built."""
    with pytest.raises(
        ImproperlyConfigured, match=r"^SpecToolset tool 'empty': SelectorSpec declares no selector"
    ):
        SpecToolset(
            {"empty": _SelectorlessDeclaration()}, descriptions={"empty": "Nothing to run."}
        )


async def test_id_property():
    assert SpecToolset({"list_widgets": list_spec()}).id == "spec-tools"
    assert SpecToolset({"list_widgets": list_spec()}, id="custom").id == "custom"


async def test_max_retries_default_and_override():
    tools = await SpecToolset({"list_widgets": list_spec()}).get_tools(None)
    assert tools["list_widgets"].max_retries == 1
    tools = await SpecToolset({"list_widgets": list_spec()}, max_retries=3).get_tools(None)
    assert tools["list_widgets"].max_retries == 3


# --- call_tool (async wrapper + identity) ------------------------------------


def _ping_spec() -> tuple[ServiceSpec, dict[str, Any]]:
    """A spec whose service records, in the dict beside it, the user it ran as."""
    seen: dict[str, Any] = {}

    def ping(user):
        """Ping."""
        seen["user"] = user
        return {"ok": True}

    return ServiceSpec(service=ping, permissions=allow(), atomic=False), seen


async def test_call_tool_uses_default_deps_user():
    spec, seen = _ping_spec()
    toolset = SpecToolset({"ping": spec})
    result = await toolset.call_tool("ping", {}, ctx_for("alice"), None)
    assert result == {"ok": True}
    assert seen["user"] == "alice"


async def test_call_tool_honours_custom_get_user():
    spec, seen = _ping_spec()
    toolset = SpecToolset(
        {"ping": spec},
        get_user=lambda ctx: ctx.principal,
    )
    await toolset.call_tool("ping", {}, ctx_for(principal="bob"), None)
    assert seen["user"] == "bob"


# --- selector dispatch -------------------------------------------------------


@pytest.mark.django_db
def test_list_selector_renders_owned_widgets():
    user = User.objects.create(username="u")
    other = User.objects.create(username="o")
    Widget.objects.create(name="a", price=1, owner=user)
    Widget.objects.create(name="b", price=2, owner=other)
    result = _dispatch(list_spec(), user, {})
    assert [w["name"] for w in _rows(result)] == ["a"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("args", "rows", "envelope"),
    [({"limit": 2}, 2, (1, 2, True)), ({"page": 2, "limit": 2}, 1, (2, 2, False))],
    ids=["limits-the-page", "second-page"],
)
def test_list_selector_pages(args, rows, envelope):
    """A sort is the spec's to declare, so this fixture is about the slice alone."""
    user = User.objects.create(username="u")
    for name, price in [("a", 3), ("b", 1), ("c", 2)]:
        Widget.objects.create(name=name, price=price, owner=user)
    result = _dispatch(list_spec(), user, args)
    assert len(_rows(result)) == rows
    assert (result["page"], result["totalPages"], result["hasNext"]) == envelope


@pytest.mark.django_db
def test_retrieve_selector_found():
    user = User.objects.create(username="u")
    widget = Widget.objects.create(name="a", price=1, owner=user)
    result = _dispatch(retrieve_spec(), user, {"pk": widget.pk})
    assert result["name"] == "a"


def _raises_does_not_exist(user, pk):
    """Fetch a single widget by primary key, raising when it is missing."""
    raise Widget.DoesNotExist(f"no widget {pk}")


def _raises_http404(user, pk):
    """The shape ``get_object_or_404`` gives a selector, without the query."""
    raise Http404(f"no widget {pk}")


#: The three ways a selector reports a missing row. Each must end as the same
#: ``ToolFailed("not found")``; the kernel's dispatch is what answers ``Http404``.
_MISSING_ROW_SELECTORS = pytest.mark.parametrize(
    "selector",
    [get_widget, _raises_does_not_exist, _raises_http404],
    ids=["empty-queryset", "does-not-exist", "http404"],
)


@pytest.mark.django_db
@_MISSING_ROW_SELECTORS
def test_retrieve_selector_not_found_fails_the_call(selector):
    """An unresolved target is ``ToolFailed``, carrying the wording unchanged."""
    user = User.objects.create(username="u")
    with pytest.raises(ToolFailed) as caught:
        _dispatch(retrieve_spec(selector=selector), user, {"pk": 999})
    assert caught.value.message == "not found"


def _list_raising_http404(user):
    """List the acting user's widgets, or 404 when there is no such parent."""
    raise Http404("no such parent")


@pytest.mark.django_db
@pytest.mark.parametrize("allow_none", [False, True], ids=["strict", "allow-none"])
def test_a_list_selector_that_raises_http404_fails_the_call(allow_none):
    """A list has no single row, so ``allow_none`` leaves its ``Http404`` a ``ToolFailed``."""
    user = User.objects.create(username="u")
    spec = list_spec(selector=_list_raising_http404, allow_none=allow_none)
    with pytest.raises(ToolFailed) as caught:
        _dispatch(spec, user, {})
    assert caught.value.message == "not found"


@pytest.mark.django_db
def test_an_http404_exception_map_entry_is_not_consulted_for_a_selector():
    """The kernel answers a selector's ``Http404`` first, so a consumer's entry is redundant."""
    user = User.objects.create(username="u")
    seen: list[BaseException] = []

    def handler(exc: BaseException) -> Any:
        seen.append(exc)
        return {"handled": True}

    with pytest.raises(ToolFailed) as caught:
        _dispatch(
            retrieve_spec(selector=_raises_http404),
            user,
            {"pk": 999},
            toolset_kwargs={"exception_map": {Http404: handler}},
        )
    assert caught.value.message == "not found"
    assert seen == []


@pytest.mark.django_db
@_MISSING_ROW_SELECTORS
def test_a_write_whose_instance_selector_finds_nothing_fails_the_call(selector):
    """The write never runs; a missing target is the same ``ToolFailed`` a read gets."""
    user = User.objects.create(username="u")
    spec = ServiceSpec(
        service=update_widget,
        validator=widget_input(),
        instance_selector_spec=by_pk(selector),
        permissions=allow(),
        atomic=False,
    )
    with pytest.raises(ToolFailed) as caught:
        _dispatch(spec, user, {"pk": 999, "name": "x", "price": 1})
    assert caught.value.message == "not found"


@pytest.mark.django_db
@_MISSING_ROW_SELECTORS
def test_a_nullable_retrieve_that_finds_nothing_returns_none(selector):
    """Not a blank row, which would read as a widget with an empty name."""
    user = User.objects.create(username="u")
    spec = retrieve_spec(allow_none=True, selector=selector)

    assert _dispatch(spec, user, {"pk": 999}) is None


@pytest.mark.django_db
def test_a_service_that_returns_nothing_returns_none():
    user = User.objects.create(username="u")
    spec = create_spec()
    spec = replace(spec, service=lambda *, data: None)

    assert _dispatch(spec, user, {"name": "a", "price": 1}) is None


# --- service dispatch --------------------------------------------------------


@pytest.mark.django_db
def test_create_service_renders_output():
    user = User.objects.create(username="u")
    result = _dispatch(create_spec(), user, {"name": "z", "price": 5})
    assert result["name"] == "z"
    assert Widget.objects.filter(name="z", owner=user).exists()


@pytest.mark.django_db
def test_create_service_validation_error_is_model_retry():
    user = User.objects.create(username="u")
    with pytest.raises(ModelRetry):
        _dispatch(create_spec(), user, {"name": "z", "price": -1})


def test_service_error_fails_the_call_with_its_own_message():
    with pytest.raises(ToolFailed) as caught:
        _dispatch(ServiceSpec(service=boom, atomic=False), object(), {})
    assert caught.value.message == "nope"


def test_service_validation_error_is_model_retry():
    with pytest.raises(ModelRetry):
        _dispatch(ServiceSpec(service=reject, atomic=False), object(), {})


# --- a service asking for input it was not given -----------------------------


def needs_confirmation(*, data=None, **_):
    """A service that discovers mid-run that it needs one more argument."""
    raise AdditionalInputRequired(
        "9412 rows match. Confirm to proceed.",
        schema={"confirmed": {"type": "boolean"}},
    )


def needs_something_unnamed(**_):
    raise AdditionalInputRequired("This needs something I cannot describe.")


def test_a_request_for_input_is_a_retry_not_a_dead_end():
    """A model can answer, and ``ModelRetry`` is already the channel to fix and call again."""
    with pytest.raises(ModelRetry) as caught:
        _dispatch(ServiceSpec(service=needs_confirmation, atomic=False), object(), {})
    assert "9412 rows match" in str(caught.value)


def test_the_retry_names_the_arguments_to_add() -> None:
    """The keys of ``schema`` are input names, the actionable part of the retry."""
    with pytest.raises(ModelRetry) as caught:
        _dispatch(ServiceSpec(service=needs_confirmation, atomic=False), object(), {})
    assert "`confirmed`" in str(caught.value)


def test_a_bare_message_still_retries() -> None:
    """``schema`` is optional upstream."""
    with pytest.raises(ModelRetry) as caught:
        _dispatch(ServiceSpec(service=needs_something_unnamed, atomic=False), object(), {})
    assert str(caught.value) == "This needs something I cannot describe."


def test_it_is_not_swallowed_by_the_generic_service_error_arm() -> None:
    """``AdditionalInputRequired`` subclasses ``ServiceError``: its arm must come first."""
    with pytest.raises(ToolFailed, match="nope"):
        _dispatch(ServiceSpec(service=boom, atomic=False), object(), {})
    with pytest.raises(ModelRetry):
        _dispatch(ServiceSpec(service=needs_confirmation, atomic=False), object(), {})


# --- permissions -------------------------------------------------------------


def test_denied_permission_raises():
    with pytest.raises(NotPermitted):
        _dispatch(list_spec(permissions=[DenyAll()]), object(), {})


@pytest.mark.django_db(transaction=True)
async def test_with_the_knob_off_only_an_unguarded_spec_runs_unchecked():
    """``require_permissions=False`` still asks a spec that declares a check."""
    user = await sync_to_async(User.objects.create)(username="u")
    await sync_to_async(Widget.objects.create)(name="mine", owner=user)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UnguardedSpecWarning)
        toolset = SpecToolset(
            {
                "unguarded": _unguarded_list_spec(),
                "guarded": list_spec(permissions=[DenyAll()]),
            },
            require_permissions=False,
        )

    answered = await toolset.call_tool("unguarded", {}, ctx_for(user), None)
    with pytest.raises(NotPermitted):
        await toolset.call_tool("guarded", {}, ctx_for(user), None)

    assert [row["name"] for row in answered["items"]] == ["mine"]


@pytest.mark.django_db
def test_a_malformed_call_is_handed_back_before_it_is_authorized():
    """Argument shape is checked before permissions, so a wrong call is a retry, not a denial."""
    with pytest.raises(ModelRetry, match=r"^name: This field is required\.$"):
        _dispatch(create_spec(permissions=[DenyAll()]), object(), {"price": 1})


@pytest.mark.parametrize("name", ["request", "view"])
def test_a_mount_registering_a_seed_the_call_supplies_is_refused(name):
    """A mount registering ``request`` or ``view`` would have it silently replaced."""
    seeds = DEFAULT_POOL_SEEDS.extend(**{name: lambda: None})

    with pytest.raises(
        ImproperlyConfigured,
        match=f"pool_seeds registers '{name}'.*the run's call_context or return it from call_seeds",
    ):
        _dispatch(_ping_spec()[0], object(), {}, toolset_kwargs={"pool_seeds": seeds})


# --- pure helpers ------------------------------------------------------------


def test_shape_list_returns_a_page_rather_than_a_bare_slice():
    """The shaper answers with a total and a clamp, and says whether more rows exist."""
    page = _shape_list([1, 2, 3, 4, 5], page=2, limit=2, max_page_size=None)
    assert page.items == [3, 4]
    assert (page.page, page.limit, page.total) == (2, 2, 5)
    assert page.total_pages == 3
    assert page.has_next is True


def test_shape_list_bounds_a_limitless_call_to_one_default_page():
    """An omitted ``limit`` is one default page, never *everything*."""
    rows = list(range(DEFAULT_PAGE_SIZE + 1))
    page = _shape_list(rows, page=None, limit=None, max_page_size=None)
    assert page.items == rows[:DEFAULT_PAGE_SIZE]
    assert page.total == DEFAULT_PAGE_SIZE + 1
    assert page.has_next is True


def test_shape_list_clamps_the_limit_to_max_page_size():
    """The clamp applies to the slice, so ``totalPages`` uses the limit actually applied."""
    page = _shape_list([1, 2, 3, 4], page=None, limit=3, max_page_size=2)
    assert page.items == [1, 2]
    assert page.limit == 2
    assert page.total_pages == 2


# --- tool-name validation ----------------------------------------------------


@pytest.mark.parametrize("bad", ["has space", "bang!", "", "x" * 65])
def test_invalid_tool_name_raises_at_construction(bad):
    with pytest.raises(ValueError, match="tool names"):
        SpecToolset({bad: list_spec()})


def test_valid_tool_names_are_accepted():
    # letters, digits, underscore, hyphen, up to 64 chars — no error.
    SpecToolset({"list_widgets-v2": list_spec()})


# --- list inputs -------------------------------------------------------------
#
# A ``many=True`` spec validates a JSON array, and a model's tool arguments are
# always a JSON object, so the list travels under the one argument the spec's
# ``many_argument`` names -- ``items`` unless it names another.


def _create_widgets(*, data, user):
    """Create several widgets for the acting user in one call."""
    return [Widget.objects.create(owner=user, **item) for item in data]


def bulk_spec(**kwargs):
    # Guarded by default, for the same reason as the fixtures above.
    kwargs.setdefault("permissions", allow())
    kwargs.setdefault("presenter", widget_presenter())
    return ServiceSpec(
        service=_create_widgets,
        validator=widget_input(),
        many=True,
        **kwargs,
    )


_ITEM = {"name": "a", "price": 1}


def test_a_many_service_spec_builds_a_tool():
    """The list travels under one argument, which is what the spec validates."""
    toolset = SpecToolset({"create_widgets": bulk_spec(), "list_widgets": list_spec()})

    assert set(toolset.specs) == {"create_widgets", "list_widgets"}


async def test_a_many_tool_takes_its_list_as_one_argument():
    tools = await SpecToolset({"create_widgets": bulk_spec()}).get_tools(None)
    schema = tools["create_widgets"].tool_def.parameters_json_schema

    assert set(schema["properties"]) == {"items"}
    assert schema["properties"]["items"]["type"] == "array"
    assert set(schema["properties"]["items"]["items"]["properties"]) == {"name", "price"}
    assert schema["required"] == ["items"]
    # The list is the whole input, and dispatch refuses anything sent beside it.
    assert schema["additionalProperties"] is False


async def test_declared_channels_are_advertised_beside_the_list_argument():
    """``additionalProperties: false`` survives the merge and forbids undeclared arguments."""
    reads = reading(
        {"create_widgets": bulk_spec()},
        {"create_widgets": [Parameter("project_pk", "string", required=True)]},
    )
    toolset = SpecToolset(
        shaping(reads.specs(), {"create_widgets": [Parameter("fields", "string")]})
    )
    tools = await toolset.get_tools(None)
    schema = tools["create_widgets"].tool_def.parameters_json_schema

    assert set(schema["properties"]) == {"items", "fields", "project_pk"}
    assert schema["required"] == ["project_pk", "items"]
    assert schema["additionalProperties"] is False


@pytest.mark.django_db
def test_declared_channels_beside_the_list_are_not_unexpected_arguments():
    """Both are declared, which keeps the refusal of any argument beside the list off them."""
    user = User.objects.create(username="u")

    result = _dispatch(
        bulk_spec(),
        user,
        {"items": [_ITEM], "fields": "name", "project_pk": "7"},
        reads=(Parameter("project_pk", "string"),),
        shapes=(Parameter("fields", "string"),),
    )

    assert [row["name"] for row in result["items"]] == ["a"]


@pytest.mark.django_db
def test_a_many_tool_creates_every_item_and_returns_the_rendered_list():
    user = User.objects.create(username="u")

    result = _dispatch(bulk_spec(), user, {"items": [_ITEM, {"name": "b", "price": 2}]})

    assert [(row["name"], row["price"]) for row in result["items"]] == [("a", 1), ("b", 2)]
    assert Widget.objects.filter(owner=user).count() == 2


@pytest.mark.django_db(transaction=True)
async def test_a_model_calls_a_many_tool_through_the_run_loop():
    user = await User.objects.acreate(username="u")

    result = await _run_one_tool(
        bulk_spec(), name="create_widgets", user=user, args={"items": [_ITEM]}
    )

    part = _tool_return(result)
    assert part.outcome == "success"
    assert [row["name"] for row in part.content["items"]] == ["a"]


@pytest.mark.django_db(transaction=True)
async def test_a_many_tool_advertises_the_rows_it_returns():
    """The two halves generated from one spec, compared against each other."""
    user = await User.objects.acreate(username="u")
    toolset = SpecToolset({"create_widgets": bulk_spec()})

    tools = await toolset.get_tools(None)
    payload = await _call(toolset, "create_widgets", user, {"items": [_ITEM]})
    return_schema = tools["create_widgets"].tool_def.return_schema

    assert return_schema is not None
    assert return_schema["type"] == "object"
    rows = return_schema["properties"]["items"]
    assert rows["type"] == "array"
    assert set(payload) == {"items"}
    assert set(payload["items"][0]) == set(rows["items"]["properties"])


@pytest.mark.django_db(transaction=True)
async def test_a_spec_naming_its_list_argument_is_advertised_and_called_by_that_name():
    user = await User.objects.acreate(username="u")
    toolset = SpecToolset({"create_widgets": bulk_spec(many_argument="widgets")})

    tools = await toolset.get_tools(None)
    schema = tools["create_widgets"].tool_def.parameters_json_schema
    payload = await _call(toolset, "create_widgets", user, {"widgets": [_ITEM]})

    assert set(schema["properties"]) == {"widgets"}
    assert schema["required"] == ["widgets"]
    assert [row["name"] for row in payload["items"]] == ["a"]


@pytest.mark.django_db
def test_an_invalid_item_is_a_retry_keyed_under_the_argument_by_its_index():
    """Only the invalid item is named, by index, under the list argument."""
    user = User.objects.create(username="u")

    with pytest.raises(ModelRetry) as retry:
        _dispatch(bulk_spec(), user, {"items": [_ITEM, {"name": "b"}]})

    detail = retry.value.__cause__.detail
    assert list(detail) == ["items"]
    assert list(detail["items"]) == [1]
    assert set(detail["items"][1]) == {"price"}
    assert not Widget.objects.exists()


@pytest.mark.django_db(transaction=True)
async def test_the_model_reads_an_item_error_as_argument_index_and_field():
    """The retry prompt names the argument, the item's index and field, then the message."""
    user = await User.objects.acreate(username="u")
    toolset = SpecToolset({"create_widgets": bulk_spec()})
    agent = Agent(
        tool_calling_model(
            "create_widgets",
            {"items": [_ITEM, {"name": "b"}]},
            retry_args={"items": [_ITEM, {"name": "b", "price": 2}]},
        ),
        deps_type=AgentDeps,
        toolsets=[toolset],
    )

    result = await agent.run("go", deps=AgentDeps(user=user))

    retries = tool_retries(result.all_messages())
    assert len(retries) == 1
    assert retries[0].content == "items[1].price: This field is required."
    assert _tool_return(result).outcome == "success"
    assert await Widget.objects.filter(owner=user).acount() == 2


@pytest.mark.django_db
def test_an_argument_beside_the_list_is_refused_under_reject():
    """The service takes only the list, so ``REJECT`` refuses the argument before any run."""
    user = User.objects.create(username="u")

    with pytest.raises(ModelRetry, match=r"^note: Unknown argument\.$"):
        _dispatch(bulk_spec(), user, {"items": [_ITEM], "note": "x"})

    assert not Widget.objects.exists()


@pytest.mark.django_db
def test_an_argument_beside_the_list_is_dropped_under_ignore():
    """``IGNORE`` drops an undeclared argument without a word and serves the list."""
    user = User.objects.create(username="u")

    _dispatch(
        bulk_spec(),
        user,
        {"items": [_ITEM], "note": "x"},
        unknown_arguments=UnknownArguments.IGNORE,
    )

    assert Widget.objects.filter(owner=user).count() == 1


def test_a_malformed_schema_fragment_is_refused_naming_the_tool():
    """The toolset reads a spec's json_schema metadata at build time, keyed by phase."""
    spec = list_spec(metadata={"json_schema": "not a mapping"})

    with pytest.raises(
        ImproperlyConfigured,
        match=r"^SpecToolset tool 'list_widgets': SelectorSpec\.metadata\['json_schema'\] must be",
    ):
        SpecToolset({"list_widgets": spec})


def test_a_schema_fragment_reaches_the_tool_s_input_schema():
    """A spec's input fragment is merged over the schema of what it takes."""
    spec = list_spec(metadata={"json_schema": {"input": {"description": "Widgets."}}})

    assert (
        _input_schema_of(SpecToolset({"list_widgets": spec}), "list_widgets")["description"]
        == "Widgets."
    )


def test_a_schema_fragment_wins_over_the_input_schema_key_by_key():
    """The input fragment merges over the derived schema one top-level key at a time."""
    spec = list_spec(metadata={"json_schema": {"input": {"additionalProperties": True}}})
    derived = _input_schema_of(
        SpecToolset({"list_widgets": list_spec()}, unknown_arguments=UnknownArguments.REJECT),
        "list_widgets",
    )

    merged = _input_schema_of(
        SpecToolset({"list_widgets": spec}, unknown_arguments=UnknownArguments.REJECT),
        "list_widgets",
    )

    assert derived["additionalProperties"] is False
    assert merged == {**derived, "additionalProperties": True}


@pytest.mark.parametrize(
    ("declared", "refusal"),
    [
        (
            {"inputs": {"title": "Widgets"}},
            r"SelectorSpec\.metadata\['json_schema'\] declares 'inputs', "
            r"which name no schema phase\.",
        ),
        (
            {"output": "Rows."},
            r"SelectorSpec\.metadata\['json_schema'\]\['output'\] must be a mapping of JSON "
            r"Schema keys to merge; got str\.",
        ),
    ],
    ids=["unknown-phase", "phase-not-a-mapping"],
)
def test_a_malformed_phase_is_refused_naming_the_tool(declared, refusal):
    """A key naming no phase, or a phase that is not a mapping, is refused at build."""
    with pytest.raises(ImproperlyConfigured, match=rf"^SpecToolset tool 'rows': {refusal}"):
        SpecToolset({"rows": list_spec(metadata={"json_schema": declared})})


def test_a_schema_fragment_reaches_the_return_schema_and_wins_key_by_key():
    """The output fragment merges over the return schema shallowly, as input does."""
    spec = list_spec(
        metadata={
            "json_schema": {
                "input": {"title": "List widgets"},
                "output": {"description": "A page of widgets.", "required": ["items"]},
            }
        }
    )
    tool = SpecToolset({"rows": spec})._tool_defs["rows"]

    assert tool.return_schema["description"] == "A page of widgets."
    assert tool.return_schema["required"] == ["items"]
    assert set(tool.return_schema["properties"]) == {"items", "page", "totalPages", "hasNext"}
    assert "title" not in tool.return_schema
    assert tool.parameters_json_schema["title"] == "List widgets"
    assert "description" not in tool.parameters_json_schema


def test_an_output_fragment_never_conjures_a_return_schema():
    """A spec declaring no output has no return schema, so an output fragment stays ``None``."""
    spec = ServiceSpec(
        service=stamp_approval,
        permissions=allow(),
        atomic=False,
        metadata={"json_schema": {"output": {"description": "Nothing."}}},
    )

    assert SpecToolset({"approve": spec})._tool_defs["approve"].return_schema is None


@dataclass
class _WidgetRow:
    name: str
    price: int


@dataclass
class _WidgetItems:
    items: list[_WidgetRow]


def _count_items(*, data):
    return {"count": len(data.items)}


@pytest.mark.django_db
def test_a_list_declared_as_a_validator_field_still_arrives():
    """The shape for a tool taking arguments beside its list, which ``many=True`` refuses."""
    user = User.objects.create(username="u")
    spec = ServiceSpec(
        service=_count_items,
        validator=django_service_specs.DataclassValidator(_WidgetItems),
        permissions=allow(),
    )
    item = {"name": "a", "price": 1}

    assert _dispatch(spec, user, {"items": [item, item]}) == {"count": 2}
    with pytest.raises(ModelRetry) as retry:
        _dispatch(spec, user, {"items": [item, {"name": "b"}]})
    items = retry.value.__cause__.detail["items"]
    assert list(items) == [1]
    assert set(items[1]) == {"price"}


# --- pagination arg validation -----------------------------------------------


@pytest.mark.django_db
def test_string_limit_is_coerced_to_int():
    user = User.objects.create(username="u")
    Widget.objects.create(owner=user, name="a", price=1)
    Widget.objects.create(owner=user, name="b", price=2)
    result = _dispatch(list_spec(), user, {"limit": "1"})
    assert len(_rows(result)) == 1


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        *(("limit", value) for value in ["abc", "2.5", "-1", 0, -3, 2.0]),
        # ``True`` is an ``int`` subclass but never a valid count.
        ("page", True),
    ],
    ids=["limit-abc", "limit-2.5", "limit--1", "limit-0", "limit--3", "limit-2.0", "bool-page"],
)
def test_a_non_positive_int_page_or_limit_is_model_retry(argument, value):
    with pytest.raises(ModelRetry, match="positive integer"):
        _dispatch(list_spec(), object(), {argument: value})


# --- unknown-arguments knob --------------------------------------------------


@pytest.mark.django_db
def test_unknown_argument_rejected_by_default():
    user = User.objects.create(username="u")
    with pytest.raises(ModelRetry, match="bogus"):
        _dispatch(create_spec(), user, {"name": "z", "price": 5, "bogus": 1})


@pytest.mark.django_db
def test_unknown_argument_ignored_when_configured():
    user = User.objects.create(username="u")
    result = _dispatch(
        create_spec(),
        user,
        {"name": "z", "price": 5, "bogus": 1},
        unknown_arguments=UnknownArguments.IGNORE,
    )
    assert result["name"] == "z"


@pytest.mark.django_db(transaction=True)
async def test_toolset_threads_the_unknown_arguments_knob():
    user = await sync_to_async(User.objects.create)(username="u")
    toolset = SpecToolset(
        {"create_widget": create_spec()}, unknown_arguments=UnknownArguments.IGNORE
    )
    out = await toolset.call_tool(
        "create_widget", {"name": "z", "price": 5, "bogus": 1}, ctx_for(user), None
    )
    assert out["name"] == "z"


@pytest.mark.parametrize(
    ("policy", "spelled"),
    [
        (UnknownArguments.PASSTHROUGH, "passthrough"),
        ("passthrough", "passthrough"),
        ("drop", "drop"),
    ],
    ids=["member", "raw-value", "drop"],
)
def test_passthrough_is_refused_when_the_toolset_is_built(policy, spelled):
    """A flat model call reaches only the arguments the spec declares."""
    with pytest.raises(
        ImproperlyConfigured,
        match=(
            rf"^SpecToolset: unknown_arguments='{spelled}' is not accepted\. "
            r"Use UnknownArguments\.REJECT or UnknownArguments\.IGNORE\. "
        ),
    ) as raised:
        SpecToolset({"list_widgets": list_spec()}, unknown_arguments=policy)

    assert "no channel" in str(raised.value)


@pytest.mark.parametrize("policy", [UnknownArguments.REJECT, UnknownArguments.IGNORE])
def test_reject_and_ignore_still_build(policy):
    """Both accepted policies, each on its own, so a narrowed allow-list fails here."""
    toolset = SpecToolset({"list_widgets": list_spec()}, unknown_arguments=policy)

    assert toolset._unknown_arguments is policy


# --- object-level permission enforcement -------------------------------------


class IsOwner(PermissionCheck):
    def has_permission(self, principal, spec):
        return True

    def has_object_permission(self, principal, spec, target):
        return target.owner_id == getattr(principal, "id", None)


def get_any_widget(pk):
    """Fetch a widget by primary key, regardless of owner."""
    return Widget.objects.filter(pk=pk)


def update_widget(instance, data):
    """Rename a widget in place."""
    instance.name = data["name"]
    instance.save(update_fields=["name"])
    return instance


@pytest.mark.django_db
def test_object_permission_denies_cross_user_retrieve():
    owner = User.objects.create(username="owner")
    other = User.objects.create(username="other")
    widget = Widget.objects.create(owner=owner, name="a", price=1)
    spec = by_pk(get_any_widget, presenter=widget_presenter(), permissions=[IsOwner()])
    with pytest.raises(NotPermitted):
        _dispatch(spec, other, {"pk": widget.pk})


@pytest.mark.django_db
def test_object_permission_allows_owner_retrieve():
    owner = User.objects.create(username="owner")
    widget = Widget.objects.create(owner=owner, name="a", price=1)
    spec = by_pk(get_any_widget, presenter=widget_presenter(), permissions=[IsOwner()])
    assert _dispatch(spec, owner, {"pk": widget.pk})["name"] == "a"


@pytest.mark.django_db
def test_object_permission_denies_cross_user_mutation():
    owner = User.objects.create(username="owner")
    other = User.objects.create(username="other")
    widget = Widget.objects.create(owner=owner, name="a", price=1)
    spec = ServiceSpec(
        service=update_widget,
        validator=widget_input(),
        instance_selector_spec=by_pk(get_any_widget),
        permissions=[IsOwner()],
        atomic=False,
    )
    # A denial aborts the run (``NotPermitted``), not a ``ModelRetry``, before any write.
    with pytest.raises(NotPermitted):
        _dispatch(spec, other, {"pk": widget.pk, "name": "hacked", "price": 9})
    widget.refresh_from_db()
    assert widget.name == "a"


# --- declaring a shape ---------------------------------------------------------


class _FieldsEcho(Presenter):
    """Reflects a read-shaping ``fields`` back, proving it reached the presenter."""

    def output(self) -> Output:
        return Output(
            (OutputField("name", "string"), OutputField("fields", "string", nullable=True))
        )

    def present(self, value: Any) -> Any:
        return self.presented(value, None)

    def presented(self, value: Any, context: Any) -> Any:
        shapes = context.shapes if context is not None else {}
        return {"name": value.name, "fields": shapes.get("fields")}


def _echo_list_spec(**kwargs):
    kwargs.setdefault("permissions", allow())
    return SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_widgets,
        presenter=_FieldsEcho(),
        **kwargs,
    )


async def test_a_query_param_read_is_told_what_it_applies_to_only_on_a_page():
    """Only the tool returning a page is told the param applies to each item, not the envelope."""
    fields = Parameter("fields", "string", help="restql field selection")
    toolset = SpecToolset(
        shaping(
            {"list_widgets": list_spec(), "get_widget": retrieve_spec()},
            {"list_widgets": [fields], "get_widget": [fields]},
        )
    )
    tools = await toolset.get_tools(None)
    list_props = tools["list_widgets"].tool_def.parameters_json_schema["properties"]
    get_props = tools["get_widget"].tool_def.parameters_json_schema["properties"]
    assert list_props["fields"] == {
        "type": "string",
        "description": "restql field selection " + _SCOPE_DESCRIPTION,
    }
    assert get_props["fields"] == {"type": "string", "description": "restql field selection"}


async def test_a_query_param_read_applies_to_the_tool_whose_spec_declares_it():
    toolset = SpecToolset(
        shaping(
            {"list_widgets": list_spec(), "get_widget": retrieve_spec()},
            {"list_widgets": [Parameter("expand", "boolean")]},
        )
    )
    tools = await toolset.get_tools(None)
    # No declared description, so the scope sentence is the whole of it.
    assert tools["list_widgets"].tool_def.parameters_json_schema["properties"]["expand"] == {
        "type": "boolean",
        "description": _SCOPE_DESCRIPTION,
    }
    get_widget_props = tools["get_widget"].tool_def.parameters_json_schema.get("properties", {})
    assert "expand" not in get_widget_props


@pytest.mark.parametrize("declare", [shaping, reading], ids=["shape", "read"])
async def test_a_read_default_appears_in_schema(declare):
    read = Parameter("fields", "string", default="id")
    toolset = SpecToolset(declare({"list_widgets": list_spec()}, {"list_widgets": [read]}))
    tools = await toolset.get_tools(None)
    props = tools["list_widgets"].tool_def.parameters_json_schema["properties"]
    assert props["fields"]["default"] == "id"


@pytest.mark.parametrize("source", ["registry", "mapping"])
def test_a_reserved_read_name_is_rejected(source):
    # Nothing sorts by a shape; advertised as ordering, it would never apply.
    registry = shaping(
        {"list_widgets": list_spec()}, {"list_widgets": [Parameter("ordering", "string")]}
    )
    with pytest.raises(
        ImproperlyConfigured, match=r"^SpecToolset tool 'list_widgets': .*\['ordering'\]"
    ):
        SpecToolset(registry if source == "registry" else registry.specs())


@pytest.mark.parametrize("source", ["registry", "mapping"])
def test_a_retrieve_tool_s_shape_may_not_be_named_page(source):
    """A shape is the presenter's on every tool, so ``page`` is refused on a retrieve tool too."""
    spec = SelectorSpec(
        kind=SelectorKind.RETRIEVE,
        selector=one_entry,
        presenter=_NameSerializer(),
        permissions=allow(),
    )
    registry = shaping({"entry": spec}, {"entry": [Parameter("page", "integer")]})

    with pytest.raises(ImproperlyConfigured, match=r"^SpecToolset tool 'entry': .*\['page'\]"):
        SpecToolset(registry if source == "registry" else registry.specs())


def one_entry():
    """The one entry there is."""
    return {"name": "only"}


@pytest.mark.parametrize("name", ["page", "limit"])
def test_a_retrieve_tool_s_route_capture_may_be_named_page_or_limit(name):
    """Only list tools paginate; on a retrieve tool, ``page`` reaches the permission check."""
    read = Parameter(name, "string")
    spec = SelectorSpec(
        kind=SelectorKind.RETRIEVE,
        selector=one_entry,
        presenter=_NameSerializer(),
        permissions=[RecordingPermission()],
    )
    toolset = SpecToolset(reading({"entry": spec}, {"entry": [read]}))

    assert name in _input_schema_of(toolset, "entry")["properties"]
    assert _dispatch(spec, None, {name: "3"}, reads=[read]) == {"name": "only"}
    assert RecordingPermission.seen["arguments"] == {name: "3"}


def test_a_route_capture_named_ordering_is_an_argument_like_any_other():
    """A read goes to permissions and the lookup, so the toolset does not take ``ordering``."""
    toolset = SpecToolset(
        reading({"list_widgets": list_spec()}, {"list_widgets": [Parameter("ordering", "string")]})
    )

    assert "ordering" in _input_schema_of(toolset, "list_widgets")["properties"]


# --- a selector parameter the toolset's own arguments take -------------------


def page_of_log(*, page: int):
    """One page of the log, numbered by the caller."""
    return [{"name": f"entry on page {page}"}]


def recent_entries(*, page: int = 1, limit: int = 10):
    """The newest entries, paged by the selector itself."""
    return [{"name": f"page {page} of {limit}"}]


def test_a_list_selector_parameter_named_page_is_refused():
    """``page`` and ``limit`` are popped before dispatch on list tools only."""
    with pytest.raises(ImproperlyConfigured, match=r"tool 'log'.*parameter\(s\) \['page'\]"):
        SpecToolset(
            {
                "log": _name_list_spec(
                    page_of_log, reads=Parameters.of(Parameter("page", "integer", required=True))
                )
            }
        )


def test_a_defaulted_page_parameter_is_refused_too():
    """Each call would run on the selector's defaults, whatever page the model asked for."""
    with pytest.raises(
        ImproperlyConfigured, match=r"parameter\(s\) \['limit', 'page'\].*pagination"
    ):
        SpecToolset(
            {
                "recent": _name_list_spec(
                    recent_entries,
                    reads=Parameters.of(
                        Parameter("page", "integer", default=1),
                        Parameter("limit", "integer", default=10),
                    ),
                )
            }
        )


def entry_on_page(*, page: int):
    """The entry printed on one page of a ledger: a page number that is data."""
    return {"name": f"entry on page {page}"}


def test_a_retrieve_selector_keeps_a_parameter_named_page():
    """Only a list tool paginates, so a retrieve selector's ``page`` is the model's to send."""
    spec = SelectorSpec(
        kind=SelectorKind.RETRIEVE,
        selector=entry_on_page,
        reads=Parameters.of(Parameter("page", "integer", required=True)),
        presenter=_NameSerializer(),
        permissions=allow(),
    )
    toolset = SpecToolset({"entry": spec})

    assert _dispatch(spec, User(username="u"), {"page": 3}) == {"name": "entry on page 3"}
    assert _input_schema_of(toolset, "entry")["required"] == ["page"]


def tasks_by_status(*, status: str = "open"):
    """The tasks in one status, the open ones when none is named."""
    return [{"name": status}]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("args", [{"status": "done"}, {}], ids=["sent", "omitted"])
async def test_a_spec_read_naming_a_selector_parameter_is_served_alike_from_both_routes(args):
    """A read of a selector parameter's name declares where that parameter comes from."""
    registry = SpecRegistry()
    registry.register(
        "tasks",
        _name_list_spec(
            tasks_by_status,
            permissions=[RecordingPermission()],
            reads=Parameters.of(Parameter("status", "string")),
        ),
    )
    from_registry = SpecToolset(registry)
    from_mapping = SpecToolset(registry.specs())

    assert _input_schema_of(from_registry, "tasks") == _input_schema_of(from_mapping, "tasks")
    served = await _call(from_registry, "tasks", AnonymousUser(), dict(args))
    seen_from_registry = RecordingPermission.seen["arguments"]
    expected = await _call(from_mapping, "tasks", AnonymousUser(), dict(args))
    assert _rows(served) == _rows(expected) == [{"name": args.get("status", "open")}]
    assert seen_from_registry == RecordingPermission.seen["arguments"]
    assert seen_from_registry == ({"status": "done"} if args else {})


class RecordingPermission(PermissionCheck):
    """Allows everything, and records what it was shown."""

    seen: dict[str, Any] = {}

    def has_permission(self, principal, spec, *, arguments, call_context, request, view):
        RecordingPermission.seen = {
            "action": call_context["action"],
            "view": view,
            "arguments": dict(arguments or {}),
            "user": principal,
            # A header only the run's request carries, so another request reads ``None``.
            "marker": request.META.get("HTTP_X_MARKER") if request is not None else None,
        }
        return True


def test_a_read_may_share_a_selector_parameters_name():
    """A read under a selector parameter's name takes that parameter's argument."""
    toolset = SpecToolset(
        reading(
            {"tasks": _name_list_spec(tasks_by_status)},
            {"tasks": [Parameter("status", "string")]},
        )
    )

    assert "status" in _input_schema_of(toolset, "tasks")["properties"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("args", "declared", "seen"),
    [
        ({"fields": "id,name"}, Parameter("fields", "string"), "id,name"),
        ({}, Parameter("fields", "string", default="id"), "id"),
        ({}, Parameter("fields", "string"), None),
    ],
    ids=["sent", "default-seeded-when-omitted", "omitted-without-default-seeds-nothing"],
)
def test_a_shape_reaches_the_presenter_under_its_default_when_left_out(args, declared, seen):
    """A shape reaches the presenter on ``PresentationContext.shapes``, at its default."""
    user = User.objects.create(username="u")
    Widget.objects.create(name="a", price=1, owner=user)
    result = _dispatch(_echo_list_spec(), user, args, shapes=(declared,))
    assert _rows(result) == [{"name": "a", "fields": seen}]


def list_widgets_selecting(user, fields=None):
    """``list_widgets``, taking ``fields`` itself."""
    return Widget.objects.filter(owner=user)


@pytest.mark.django_db
def test_a_read_a_callable_takes_is_its_argument_and_never_the_presenter_s():
    """A read the selector takes by name is its argument, not one of the spec's shapes."""
    user = User.objects.create(username="u")
    Widget.objects.create(name="a", price=1, owner=user)
    spec = replace(_echo_list_spec(), selector=list_widgets_selecting)

    result = _dispatch(spec, user, {"fields": "id"}, reads=(Parameter("fields", "string"),))

    assert _rows(result) == [{"name": "a", "fields": None}]


# What a ``**kwargs`` selector, a selector naming ``fields`` and a ``kwargs=``
# provider naming it were handed, one entry per call.
_TAKEN: list[tuple[str, Any]] = []


def list_widgets_filtered(user, **filters):
    """``list_widgets``, taking whatever filters it is sent."""
    _TAKEN.append(("selector-kwargs", filters.get("fields", "absent")))
    return Widget.objects.none()


def _names_naming_fields(user, fields=None):
    """Names, taking ``fields`` itself."""
    _TAKEN.append(("selector-named", fields))
    return [{"name": "x"}]


def _names_taking_anything(user, **filters):
    """Names, taking whatever filters it is sent."""
    _TAKEN.append(("selector-kwargs", filters.get("fields", "absent")))
    return [{"name": "x"}]


def _names_under_a_ceiling(user, ceiling=None):
    """Names, under a ceiling a provider fills."""
    return [{"name": "x"}]


def _ceiling_from_fields(fields=None):
    _TAKEN.append(("provider", fields))
    return {"ceiling": 1}


@pytest.mark.django_db
async def test_a_read_a_kwargs_selector_takes_is_its_argument_with_its_default():
    """A read taken by a ``**kwargs`` selector is a plain argument, not a per-item shape."""
    _TAKEN.clear()
    spec = replace(_echo_list_spec(), selector=list_widgets_filtered)
    fields = Parameter("fields", "string", default="id", help="Fields to return.")
    toolset = SpecToolset(reading({"w": spec}, {"w": [fields]}), descriptions={"w": "W."})

    assert _input_schema_of(toolset, "w")["properties"]["fields"] == {
        "type": "string",
        "default": "id",
        "description": "Fields to return.",
    }
    assert "read-shaping" not in str(await toolset.get_instructions(ctx_for(User(username="u"))))
    await _call(toolset, "w", User(username="u"), {})
    assert _TAKEN == [("selector-kwargs", "id")]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("spec_kwargs", "taken"),
    [
        ({"selector": _names_naming_fields}, [("selector-named", "id")]),
        pytest.param(
            {"selector": _names_taking_anything},
            [("selector-kwargs", "id")],
        ),
        pytest.param(
            {"selector": _names_under_a_ceiling, "kwargs": _ceiling_from_fields},
            [("provider", "id")],
        ),
    ],
    ids=["selector-named", "selector-kwargs", "provider"],
)
async def test_an_explicit_null_for_a_read_a_callable_takes_is_treated_as_omitted(
    spec_kwargs, taken
):
    """A ``null`` sent for a read is the model declining it, dropped as an omitted read is."""
    _TAKEN.clear()
    spec = _name_list_spec(**spec_kwargs)
    toolset = SpecToolset(
        reading({"t": spec}, {"t": [Parameter("fields", "string", default="id")]}),
        descriptions={"t": "T."},
    )

    await _call(toolset, "t", User(username="u"), {"fields": None})

    assert taken == _TAKEN


@pytest.mark.django_db
async def test_a_read_a_selector_parameter_takes_is_handed_the_default_it_advertises():
    """The read's declaration stands for its parameter, so a call omitting it gets the default."""
    _TAKEN.clear()
    toolset = SpecToolset(
        reading(
            {"t": _name_list_spec(selector=_names_naming_fields)},
            {"t": [Parameter("fields", "string", default="id")]},
        ),
        descriptions={"t": "T."},
    )

    assert _input_schema_of(toolset, "t")["properties"]["fields"]["default"] == "id"
    await _call(toolset, "t", User(username="u"))
    assert _TAKEN == [("selector-named", "id")]


@pytest.mark.django_db
async def test_a_read_s_default_never_replaces_a_value_the_model_sent():
    """A default stands in for a value left out, never for one sent."""
    _TAKEN.clear()
    toolset = SpecToolset(
        reading(
            {"t": _name_list_spec(selector=_names_naming_fields)},
            {"t": [Parameter("fields", "string", default="id")]},
        ),
        descriptions={"t": "T."},
    )

    await _call(toolset, "t", User(username="u"), {"fields": "name"})

    assert _TAKEN == [("selector-named", "name")]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("declared", "seen"),
    [(Parameter("fields", "string", default="id"), "id"), (Parameter("fields", "string"), None)],
    ids=["default applies", "no default"],
)
def test_an_explicit_null_shape_is_treated_as_omitted(declared, seen):
    """``{\"fields\": null}`` is a model declining to fill the param."""
    user = User.objects.create(username="u")
    Widget.objects.create(name="a", price=1, owner=user)
    result = _dispatch(
        _echo_list_spec(),
        user,
        {"fields": None},
        shapes=(declared,),
        unknown_arguments=UnknownArguments.REJECT,
    )
    assert _rows(result) == [{"name": "a", "fields": seen}]


@pytest.mark.django_db
def test_a_declared_shape_is_no_unknown_argument_under_reject():
    # A closed-input list selector under REJECT: an undeclared arg would raise ModelRetry.
    user = User.objects.create(username="u")
    Widget.objects.create(name="a", price=1, owner=user)
    result = _dispatch(
        list_spec(),
        user,
        {"fields": "x"},
        shapes=(Parameter("fields", "string"),),
        unknown_arguments=UnknownArguments.REJECT,
    )
    assert [w["name"] for w in _rows(result)] == ["a"]


# --- full agent-run integration ----------------------------------------------
#
# Drive a real ``Agent`` run loop (FunctionModel — no provider, no network) to
# pin the toolset's run-loop contract on the locked pydantic-ai: the tools
# execute in-process (``call_tool`` is invoked and the run completes, rather
# than the call being deferred to the client), and a ``ModelRetry`` is fed back
# to the model to self-correct instead of aborting the run.


async def test_agent_run_executes_spec_tool_in_process():
    spec, seen = _ping_spec()
    toolset = SpecToolset({"ping": spec})
    agent = Agent(tool_calling_model("ping"), deps_type=AgentDeps, toolsets=[toolset])
    result = await agent.run("go", deps=AgentDeps(user="alice"))
    assert result.output == "done"
    assert seen["user"] == "alice"


async def test_toolset_instructions_reach_the_model_when_attached_directly():
    # A plain Agent with SpecToolset in ``toolsets=`` gets the conventions too.
    captured: dict[str, Any] = {}
    agent = Agent(
        instruction_capturing_model(captured),
        deps_type=AgentDeps,
        toolsets=[SpecToolset({"list": list_spec()})],
    )
    result = await agent.run("go", deps=AgentDeps(user="alice"))
    assert result.output == "done"
    assert captured["instructions"] is not None
    assert _DEFAULTS.base in captured["instructions"]
    assert _pagination_line(DEFAULT_PAGE_SIZE) in captured["instructions"]


@dataclass
class _ModeInput:
    mode: str


async def test_agent_run_recovers_from_model_retry():
    calls = []

    def flaky(data, user):
        """Fails validation on the bad mode, succeeds otherwise."""
        calls.append(data.mode)
        if data.mode == "bad":
            raise ServiceValidationError("mode must not be 'bad'")
        return {"ok": True}

    toolset = SpecToolset(
        {
            "flaky": ServiceSpec(
                service=flaky,
                validator=django_service_specs.DataclassValidator(_ModeInput),
                permissions=allow(),
                atomic=False,
            )
        }
    )
    agent = Agent(
        tool_calling_model("flaky", {"mode": "bad"}, retry_args={"mode": "good"}),
        deps_type=AgentDeps,
        toolsets=[toolset],
    )
    result = await agent.run("go", deps=AgentDeps(user="alice"))
    assert result.output == "done"
    assert calls == ["bad", "good"]


# ----- the outcome a terminal failure is marked with -----
#
# The assertions that only a real run can make. ``pytest.raises(ToolFailed)``
# above proves the arm raises; it says nothing about what pydantic-ai does with
# the exception, and the whole point of raising rather than returning is the
# ``outcome`` it produces on the resulting ``ToolReturnPart``. That field is what
# a transport forwards, so a test asserting only the message would keep passing
# if the marking regressed to ``"success"``.


def _tool_return(result: Any) -> ToolReturnPart:
    """The one tool return in a finished run's messages that is not a retry."""
    returns = tool_answers(result.all_messages())
    assert len(returns) == 1, f"expected exactly one tool return, got {len(returns)}"
    return returns[0]


async def _run_one_tool(
    spec: Any,
    *,
    name: str = "t",
    user: Any = "alice",
    args: dict[str, Any] | None = None,
    **toolset_kwargs: Any,
) -> Any:
    """One agent run whose model calls ``name`` once and then answers."""
    toolset = SpecToolset({name: spec}, **toolset_kwargs)
    agent = Agent(tool_calling_model(name, args), deps_type=AgentDeps, toolsets=[toolset])
    return await agent.run("go", deps=AgentDeps(user=user))


async def test_a_business_failure_marks_the_tool_return_failed():
    """A ``ServiceError`` reaches the wire as a failed call, never a successful one."""
    result = await _run_one_tool(ServiceSpec(service=boom, permissions=allow(), atomic=False))
    part = _tool_return(result)
    assert part.outcome == "failed"
    assert part.content == "nope"
    assert result.output == "done"


async def test_a_refused_call_marks_the_tool_return_failed_and_carries_its_code():
    """An ``ActionUnavailable`` goes on the wire marked failed, like any business error."""
    books = {"open": True}
    spec = ServiceSpec(
        service=_refusing_service,
        atomic=False,
        affordances=[
            Affordance(
                code="books_closed", reason="The books are closed.", when=lambda: books["open"]
            )
        ],
        permissions=allow(),
    )
    calls_close_books = tool_calling_model("close_books")

    def close_the_books_then_call(messages: Any, info: Any) -> Any:
        books["open"] = False
        return calls_close_books.function(messages, info)

    agent = Agent(
        FunctionModel(close_the_books_then_call),
        deps_type=AgentDeps,
        toolsets=[SpecToolset({"close_books": spec})],
    )
    result = await agent.run("go", deps=AgentDeps(user="alice"))
    part = _tool_return(result)
    assert part.outcome == "failed"
    assert part.content == "The books are closed. (code: books_closed)"
    assert result.output == "done"


@pytest.mark.django_db(transaction=True)
@_MISSING_ROW_SELECTORS
async def test_an_unresolved_target_marks_the_tool_return_failed(selector):
    user = await sync_to_async(User.objects.create)(username="alice")
    result = await _run_one_tool(retrieve_spec(selector=selector), user=user, args={"pk": 999})
    part = _tool_return(result)
    assert part.outcome == "failed"
    assert part.content == "not found"


def _crawl(user):
    """Takes longer than the deadline."""
    time.sleep(0.2)
    return {}


async def test_a_dispatch_past_its_deadline_marks_the_tool_return_failed():
    result = await _run_one_tool(
        ServiceSpec(service=_crawl, permissions=allow(), atomic=False),
        name="crawl",
        dispatch_timeout=0.01,
    )
    part = _tool_return(result)
    assert part.outcome == "failed"
    assert "was abandoned" in str(part.content)


@pytest.mark.django_db(transaction=True)
async def test_a_result_over_the_ceiling_marks_the_tool_return_failed():
    """The dispatch succeeded and the *result* was refused."""
    user = await sync_to_async(User.objects.create)(username="alice")
    await sync_to_async(Widget.objects.create)(name="a", price=1, owner=user)

    result = await _run_one_tool(list_spec(), name="list_widgets", user=user, max_result_bytes=20)
    part = _tool_return(result)
    assert part.outcome == "failed"
    assert "byte ceiling" in str(part.content)


async def test_an_exception_map_handler_that_returns_a_value_still_succeeds():
    """``exception_map`` returns *the tool's result*, which pydantic-ai marks ``success``."""
    result = await _run_one_tool(
        ServiceSpec(service=boom, permissions=allow(), atomic=False),
        exception_map={ServiceError: lambda exc: {"matches": []}},
    )
    part = _tool_return(result)
    assert part.outcome == "success"
    assert part.content == {"matches": []}


# --- filter_set needs no declared read -----------------------------------------


class _WidgetFilterSet(django_filters.FilterSet):
    min_price = django_filters.NumberFilter(field_name="price", lookup_expr="gte")

    class Meta:
        model = Widget
        fields = []


def _filtered_list_spec():
    return SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_widgets,
        presenter=widget_presenter(),
        filter_set=_WidgetFilterSet,
        permissions=allow(),
    )


async def test_filter_set_fields_are_auto_exposed_as_tool_args():
    # A ``filter_set`` selector's fields reach the schema via the kernel, with no read declared.
    toolset = SpecToolset({"list_widgets": _filtered_list_spec()})
    tools = await toolset.get_tools(None)
    assert "min_price" in tools["list_widgets"].tool_def.parameters_json_schema["properties"]


@pytest.mark.django_db
def test_filter_set_filters_via_ordinary_params_not_query_params():
    user = User.objects.create(username="u")
    Widget.objects.create(name="cheap", price=1, owner=user)
    Widget.objects.create(name="pricey", price=10, owner=user)
    # A ``filter_set``'s declared set is open, so dispatch hands the value to the FilterSet.
    result = _dispatch(_filtered_list_spec(), user, {"min_price": "5"})
    assert [w["name"] for w in _rows(result)] == ["pricey"]


@pytest.mark.django_db
def test_reject_refuses_a_key_outside_a_filter_sets_surface():
    """A ``filter_set``'s filters are declared names: an unknown key is refused under REJECT."""
    with pytest.raises(ModelRetry, match=r"^colour: Unknown argument\.$"):
        _dispatch(_filtered_list_spec(), User(username="u"), {"colour": "red"})


def _open_selector(user, **kwargs):
    """Every widget, whatever is asked for."""
    _OPEN_SELECTOR_CALLS.append(dict(kwargs))
    return [{"name": "all"}]


_OPEN_SELECTOR_CALLS: list[dict[str, Any]] = []


def _open_list_spec() -> SelectorSpec:
    return SelectorSpec(
        kind=SelectorKind.LIST,
        selector=_open_selector,
        presenter=_NameSerializer(),
        permissions=allow(),
    )


@pytest.mark.django_db
def test_a_bare_kwargs_selector_is_handed_nothing_undeclared():
    """Under ``IGNORE`` a key no parameter declares is dropped, not handed to the callable."""
    _OPEN_SELECTOR_CALLS.clear()

    result = _dispatch(
        _open_list_spec(),
        User(username="u"),
        {"colour": "red"},
        unknown_arguments=UnknownArguments.IGNORE,
    )

    assert _rows(result) == [{"name": "all"}]
    (call,) = _OPEN_SELECTOR_CALLS
    assert "colour" not in call


@pytest.mark.django_db
def test_a_bare_kwargs_selector_declares_no_name_so_reject_refuses_every_key():
    """The kernel closes the argument set at the declared parameters, not a bare ``**kwargs``."""
    _OPEN_SELECTOR_CALLS.clear()

    with pytest.raises(ModelRetry, match=r"^colour: Unknown argument\.$"):
        _dispatch(_open_list_spec(), User(username="u"), {"colour": "red"})

    assert _OPEN_SELECTOR_CALLS == []


# --- reads the selector does not take ------------------------------------------


async def test_a_read_is_advertised_alike_on_every_tool_declaring_it():
    project = Parameter("project_pk", "integer", help="owning project")
    toolset = SpecToolset(
        reading(
            {"list_widgets": list_spec(), "get_widget": retrieve_spec()},
            {"list_widgets": [project], "get_widget": [project]},
        )
    )
    tools = await toolset.get_tools(None)
    for name in ("list_widgets", "get_widget"):
        props = tools[name].tool_def.parameters_json_schema["properties"]
        assert props["project_pk"] == {"type": "integer", "description": "owning project"}


async def test_a_read_applies_to_the_tool_whose_spec_declares_it():
    toolset = SpecToolset(
        reading(
            {"list_widgets": list_spec(), "get_widget": retrieve_spec()},
            {"list_widgets": [Parameter("parent_pk", "string")]},
        )
    )
    tools = await toolset.get_tools(None)
    assert "parent_pk" in tools["list_widgets"].tool_def.parameters_json_schema["properties"]
    get_widget_props = tools["get_widget"].tool_def.parameters_json_schema.get("properties", {})
    assert "parent_pk" not in get_widget_props


def _ceiling_from_project(project_pk=None):
    """Scoping provider: derive a price ceiling from the call's ``project_pk``."""
    return {"ceiling": int(project_pk) if project_pk is not None else 0}


def list_under_ceiling(user, ceiling):
    """List the user's widgets priced at or below the project's ceiling."""
    return Widget.objects.filter(owner=user, price__lte=ceiling)


def _provider_scoped_spec():
    # ``project_pk`` is consumed by the provider by name; the selector never declares it.
    return SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_under_ceiling,
        presenter=widget_presenter(),
        kwargs=_ceiling_from_project,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("args", "declared", "names"),
    [
        ({"project_pk": "10"}, Parameter("project_pk", "string"), ["cheap"]),
        ({}, Parameter("project_pk", "string", default="10"), ["cheap"]),
        # No project_pk, no default: the provider is handed none, the ceiling is 0.
        ({}, Parameter("project_pk", "string"), []),
    ],
    ids=["sent", "default-seeded-when-omitted", "omitted-without-default-seeds-nothing"],
)
def test_a_read_reaches_a_scoping_provider_by_name(args, declared, names):
    user = User.objects.create(username="u")
    Widget.objects.create(name="cheap", price=5, owner=user)
    Widget.objects.create(name="dear", price=15, owner=user)
    result = _dispatch(_provider_scoped_spec(), user, args, reads=(declared,))
    assert [w["name"] for w in _rows(result)] == names


@pytest.mark.django_db
def test_a_read_only_a_provider_takes_is_no_unknown_argument_under_reject():
    # The selector takes no ``project_pk``; under REJECT an argument nothing declares would raise.
    user = User.objects.create(username="u")
    Widget.objects.create(name="cheap", price=5, owner=user)
    result = _dispatch(
        _provider_scoped_spec(),
        user,
        {"project_pk": "10"},
        reads=(Parameter("project_pk", "string"),),
        unknown_arguments=UnknownArguments.REJECT,
    )
    assert [w["name"] for w in _rows(result)] == ["cheap"]


class _ProjectExtras(TypedDict, total=False):
    project_pk: int


def list_in_project(user, **extras: Unpack[_ProjectExtras]):
    """List the user's widgets in the given project (price ceiling as proxy)."""
    pk = extras.get("project_pk")
    qs = Widget.objects.filter(owner=user)
    return qs.filter(price__lte=pk) if pk is not None else qs.none()


def _dual_declared_spec():
    """A list over ``list_in_project``, whose ``project_pk`` is the selector's own."""
    return SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_in_project,
        reads=Parameters.of(Parameter("project_pk", "integer", reflected=True)),
        presenter=widget_presenter(),
        permissions=allow(),
    )


@pytest.mark.django_db
def test_a_reflected_extras_key_reaches_a_provider_naming_it_too():
    """A provider reading a name by argument needs no read of its own to be handed it."""
    seen = {}

    def scope_provider(project_pk=None):
        seen["project_pk"] = project_pk
        return {}

    user = User.objects.create(username="u")
    Widget.objects.create(name="cheap", price=5, owner=user)
    spec = replace(_dual_declared_spec(), kwargs=scope_provider)
    result = _dispatch(spec, user, {"project_pk": 10})
    assert [w["name"] for w in _rows(result)] == ["cheap"]
    assert seen == {"project_pk": 10}


@pytest.mark.django_db
def test_a_declared_read_reaches_the_selector_s_extras():
    user = User.objects.create(username="u")
    Widget.objects.create(name="cheap", price=5, owner=user)
    Widget.objects.create(name="dear", price=15, owner=user)
    # Declared on the spec, the read is typed by its own declaration and reaches the selector.
    result = _dispatch(
        replace(_dual_declared_spec(), reads=Parameters.of(Parameter("project_pk", "integer"))),
        user,
        {"project_pk": 10},
    )
    assert [w["name"] for w in _rows(result)] == ["cheap"]


# --- required reads ------------------------------------------------------------


async def test_a_required_read_is_advertised_as_required():
    toolset = SpecToolset(
        reading(
            {"list_widgets": list_spec()},
            {"list_widgets": [Parameter("project_pk", "integer", required=True)]},
        )
    )
    tools = await toolset.get_tools(None)
    schema = tools["list_widgets"].tool_def.parameters_json_schema
    assert schema["required"] == ["project_pk"]


def test_a_required_read_is_required_from_a_bare_mapping_too():
    """The schema is the spec's, so a mapping-built tool requires what its spec requires."""
    spec = replace(
        list_spec(), reads=Parameters.of(Parameter("project_pk", "integer", required=True))
    )

    schema = _input_schema_of(SpecToolset({"list_widgets": spec}), "list_widgets")

    assert schema["required"] == ["project_pk"]


def test_omitting_a_required_read_raises_model_retry():
    # Models ignore ``required``, so a missing argument gets a ``ModelRetry`` naming it.
    with pytest.raises(ModelRetry, match="project_pk"):
        _dispatch(list_spec(), None, {}, reads=[Parameter("project_pk", "string", required=True)])


@pytest.mark.django_db
def test_supplying_a_required_read_dispatches_normally():
    user = User.objects.create(username="u")
    Widget.objects.create(name="a", price=1, owner=user)
    result = _dispatch(
        list_spec(),
        user,
        {"project_pk": "P1"},
        reads=[Parameter("project_pk", "string", required=True)],
    )
    assert [w["name"] for w in _rows(result)] == ["a"]


def by_label(*, label: str):
    """The widgets under one label, a string the caller names."""
    return Widget.objects.filter(name=label)


def test_a_call_wrong_in_two_places_is_told_both_at_once():
    """A mistyped lookup value and a missing field get one retry naming both."""
    spec = _update_spec(
        instance_selector_spec=SelectorSpec(
            kind=SelectorKind.RETRIEVE,
            selector=by_label,
            reads=Parameters.of(Parameter("label", "string", required=True)),
        )
    )

    with pytest.raises(ModelRetry) as retry:
        _dispatch(spec, None, {"label": 1, "price": 1})

    assert retry.value.message == "label: Expected a string.\nname: This field is required."


@pytest.mark.django_db
def test_a_required_read_with_a_default_is_served_under_its_default():
    """An absent value of a required read with a default is not refused: the default is it."""
    read = Parameter("project_pk", "string", default="P1", required=True)
    spec = list_spec(permissions=[RecordingPermission()])
    SpecToolset(reading({"list_widgets": spec}, {"list_widgets": [read]}))

    _dispatch(spec, None, {}, reads=[read])

    assert RecordingPermission.seen["arguments"] == {"project_pk": "P1"}


# Wording for refusals of channel declarations the toolset does not make.
def _STRING(name: str) -> Parameter:  # noqa: N802 - reads as the declaration it builds
    return Parameter(name, "string")


_REFUSALS_THE_TOOLSET_DOES_NOT_MAKE = (
    "takes its list under the argument",
    "that the tool also declares as a QueryParam",
    "cannot route to two channels",
    "which a UrlKwarg declared for the tool requires",
)


@pytest.mark.parametrize(
    ("build", "refusal"),
    [
        pytest.param(
            lambda: SpecToolset(shaping({"c": bulk_spec()}, {"c": [_STRING("items")]})),
            r"Parameters \['items'\] are declared by two sources",
            id="shape-named-after-the-list-argument",
        ),
        pytest.param(
            lambda: SpecToolset(reading({"c": bulk_spec()}, {"c": [_STRING("items")]})),
            r"Parameters \['items'\] are declared by two sources",
            id="read-named-after-the-list-argument",
        ),
        pytest.param(
            lambda: SpecToolset(reading({"make": create_spec()}, {"make": [_STRING("name")]})),
            r"Parameters \['name'\] are declared by two sources",
            id="read-naming-a-validator-field",
        ),
        pytest.param(
            lambda: SpecToolset(
                shaping(
                    reading({"l": list_spec()}, {"l": [_STRING("scope")]}).specs(),
                    {"l": [_STRING("scope")]},
                )
            ),
            r"scope",
            id="one-name-a-read-and-a-shape",
        ),
        pytest.param(
            lambda: SpecToolset(reading({"l": list_spec()}, {"l": [_STRING("user")]})),
            r"declares the parameter\(s\) \['user'\], which dispatch seeds itself",
            id="a-default-pool-seed",
        ),
        pytest.param(
            lambda: SpecToolset(
                reading(
                    {"l": list_spec()}, {"l": [Parameter("project_pk", "string", required=True)]}
                ),
                tool_server_arguments={"l": ServerArguments(withheld=frozenset({"project_pk"}))},
            ),
            r"withholds project_pk, which no call could then do without",
            id="a-required-read-withheld",
        ),
    ],
)
def test_a_read_no_spec_can_declare_is_refused_before_the_toolset_checks_it(build, refusal):
    """The kernel refuses each of these declarations at build, so a toolset never serves one."""
    with pytest.raises(ImproperlyConfigured, match=refusal) as raised:
        build()

    assert not any(wording in str(raised.value) for wording in _REFUSALS_THE_TOOLSET_DOES_NOT_MAKE)


# --- the host absolute URLs are made against ---------------------------------


class _Fileish(Presenter):
    """Renders a file's URL absolute against the call's ``request``, relative without one."""

    def output(self) -> Output:
        return Output(
            (
                OutputField("id", "integer"),
                OutputField("name", "string"),
                OutputField("doc_url", "string"),
            )
        )

    def present(self, value: Any) -> Any:
        return self.presented(value, None)

    def presented(self, value: Any, context: Any) -> Any:
        request = context.call_context.get("request") if context is not None else None
        url = "/media/doc.pdf"
        if request is not None:
            url = request.build_absolute_uri(url)
        return {"id": value.id, "name": value.name, "doc_url": url}


def fileish_spec():
    return SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_widgets,
        presenter=_Fileish(),
        permissions=allow(),
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("arrived", "url"),
    [
        (
            {"HTTP_HOST": "app.example.com:8000"},
            "http://app.example.com:8000/media/doc.pdf",
        ),
        (
            {"HTTP_HOST": "files.example.com", "secure": True},
            "https://files.example.com/media/doc.pdf",
        ),
        (None, "/media/doc.pdf"),
    ],
    ids=["its-host-makes-file-urls-absolute", "its-scheme-too", "without-one-they-stay-relative"],
)
async def test_the_request_a_run_arrived_on_decides_whether_file_urls_are_absolute(arrived, url):
    """The host a URL is made absolute against is the request the run arrived on."""
    user = await sync_to_async(User.objects.create)(username="u")
    await sync_to_async(Widget.objects.create)(name="a", price=1, owner=user)
    deps = AgentDeps(user=user)
    if arrived is not None:
        deps.call_context = {"request": RequestFactory().get("/chat/", **arrived)}
    toolset = SpecToolset({"list_widgets": fileish_spec()})
    tools = await toolset.get_tools(None)
    result = await toolset.call_tool(
        "list_widgets", {}, ctx_for(user, deps=deps), tools["list_widgets"]
    )
    assert [w["doc_url"] for w in _rows(result)] == [url]


# ----- registration-time honesty: permissions and descriptions -----


def _unguarded_list_spec():
    """Deliberately without ``permission_classes`` — the shape under test."""
    return SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_widgets,
        presenter=widget_presenter(),
    )


def test_an_unguarded_spec_is_refused_by_default():
    """Over HTTP ``None`` means *inherit*; off HTTP there is nothing to inherit."""
    with pytest.raises(ImproperlyConfigured) as excinfo:
        SpecToolset({"list_widgets": _unguarded_list_spec()})

    message = str(excinfo.value)
    assert "'list_widgets'" in message
    assert "require_permissions=False" in message


def test_every_unguarded_spec_is_named_at_once():
    """One construction, one list — not one error per fix-and-rerun cycle."""
    with pytest.raises(ImproperlyConfigured) as excinfo:
        SpecToolset({"a": _unguarded_list_spec(), "b": _unguarded_list_spec()})

    assert "'a', 'b'" in str(excinfo.value)


def test_require_permissions_false_downgrades_to_a_warning():
    with pytest.warns(UnguardedSpecWarning, match="list_widgets"):
        toolset = SpecToolset({"list_widgets": _unguarded_list_spec()}, require_permissions=False)

    assert toolset.id == "spec-tools"


def test_a_guarded_spec_says_nothing(recwarn):
    SpecToolset({"list_widgets": list_spec()})
    assert [w for w in recwarn.list if issubclass(w.category, UnguardedSpecWarning)] == []


def test_an_explicitly_unrestricted_spec_is_neither_refused_nor_warned_about(recwarn):
    """``Unrestricted()`` declares anyone may call; no permissions at all is refused."""
    SpecToolset({"list_widgets": list_spec(permissions=[Unrestricted()])})

    assert [w for w in recwarn.list if issubclass(w.category, UnguardedSpecWarning)] == []


async def test_a_description_override_reaches_the_tool_def():
    """The docstring an API developer reads is not the sentence a model needs."""
    toolset = SpecToolset(
        {"list_widgets": list_spec()},
        descriptions={"list_widgets": "Use when the user asks what they own."},
    )
    tools = await toolset.get_tools(None)
    assert tools["list_widgets"].tool_def.description == "Use when the user asks what they own."


def test_a_description_for_an_unknown_tool_is_a_typo():
    """Dropping it silently would leave the tool with the text it was meant to lose."""
    with pytest.raises(ValueError, match="unknown tool 'list_widget'"):
        SpecToolset({"list_widgets": list_spec()}, descriptions={"list_widget": "…"})


def test_a_tool_with_no_description_anywhere_warns():
    with pytest.warns(UndescribedToolWarning, match="'empty'"):
        SpecToolset({"empty": _undescribed_spec()})


def test_a_whitespace_only_description_counts_as_none():
    """Otherwise the override is a way to silence the warning without fixing it."""
    with pytest.warns(UndescribedToolWarning, match="'list_widgets'"):
        SpecToolset({"list_widgets": list_spec()}, descriptions={"list_widgets": "   "})


# ----- ordering: a tool that advertises none accepts none -----
#
# The toolset contributes no sort argument of its own. A list selector that
# declares nothing therefore has none, and a model that sends one is told so
# rather than having the value fall through to the spec.


@pytest.mark.django_db
@pytest.mark.parametrize(
    "ordering", ["name", ["price"]], ids=["a-string", "a-non-string-is-refused-the-same-way"]
)
def test_ordering_on_a_tool_that_declares_none_says_so(ordering):
    """The tool schema is advisory, so a value of any shape reaches the pop untyped."""
    with pytest.raises(ModelRetry, match="does not accept an `ordering` argument"):
        _dispatch(list_spec(), object(), {"ordering": ordering})


async def test_the_ordering_instruction_is_absent_when_no_tool_advertises_a_sort():
    """Advice a model cannot act on spends prompt budget on an argument it will be refused."""
    without = await SpecToolset({"list_widgets": list_spec()}).get_instructions(None)
    assert "accept `ordering`" not in without


# ----- ordering: the filter_set owns it -----
#
# The one vocabulary, and the whole of it. A FilterSet carrying an
# ``OrderingFilter`` advertises its sort to the model on its own —
# ``OrderingFilter`` subclasses ``ChoiceFilter``, which the kernel's schema maps
# to a labelled set of options — and validates and applies the value itself. The
# toolset contributes nothing and takes nothing away.


class _OrderedWidgetFilterSet(django_filters.FilterSet):
    """Public sort names that are *not* the ORM paths they resolve to."""

    min_price = django_filters.NumberFilter(field_name="price", lookup_expr="gte")
    ordering = django_filters.OrderingFilter(fields=(("price", "cost"), ("name", "title")))

    class Meta:
        model = Widget
        fields = []


def _ordered_list_spec():
    return SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_widgets,
        presenter=widget_presenter(),
        filter_set=_OrderedWidgetFilterSet,
        permissions=allow(),
    )


class _SortingWidgetFilterSet(django_filters.FilterSet):
    """The same sort, declared under a name that is not ``ordering``."""

    sorting = django_filters.OrderingFilter(fields=(("price", "cost"), ("name", "title")))

    class Meta:
        model = Widget
        fields = []


def _sorting_list_spec():
    return SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_widgets,
        presenter=widget_presenter(),
        filter_set=_SortingWidgetFilterSet,
        permissions=allow(),
    )


def _three_widgets(user):
    """Insertion order a, b, c; price order b, c, a — so the two are tellable apart."""
    for name, price in [("a", 3), ("b", 1), ("c", 2)]:
        Widget.objects.create(name=name, price=price, owner=user)


def test_the_sort_argument_is_found_under_whatever_name_it_was_declared():
    """The name is the project's to choose, and nothing requires ``ordering``."""
    assert _spec_ordering_argument(_sorting_list_spec()) == "sorting"


def test_a_plain_filter_is_not_mistaken_for_a_sort():
    # min_price is a NumberFilter.
    assert _spec_ordering_argument(_filtered_list_spec()) is None


_FILTER_VALUES_REACH_KWARGS = pytest.mark.xfail(
    strict=True,
    reason=(
        "The kernel hands a filter_set's values, the ordering among them, to a "
        "selector's **kwargs catch-all; they belong to the filter step, and the "
        "kernel should withhold them as it withholds call_context."
    ),
)


@_FILTER_VALUES_REACH_KWARGS
@pytest.mark.django_db
def test_a_renamed_sort_is_popped_out_of_the_callables_kwargs():
    """Popped, not merely applied."""
    seen: dict[str, object] = {}

    def recording_selector(**kwargs):
        seen.update(kwargs)
        return Widget.objects.all()

    spec = SelectorSpec(
        kind=SelectorKind.LIST,
        selector=recording_selector,
        presenter=widget_presenter(),
        filter_set=_SortingWidgetFilterSet,
        permissions=allow(),
    )
    user = User.objects.create(username="u")
    _three_widgets(user)

    result = _dispatch(spec, user, {"sorting": ["cost"]})

    assert "sorting" not in seen, "the sort reached the selector as a surprise kwarg"
    assert [w["name"] for w in _rows(result)] == ["b", "c", "a"]


def test_the_instruction_names_the_argument_that_actually_exists():
    """A hard-coded name in the prose is a second copy of the same assumption."""
    instructions = _derive_instructions({"list_widgets": _sorting_list_spec()}, {})

    assert "`sorting`" in instructions
    assert "`ordering`" not in instructions


def test_the_instruction_names_every_live_sort_argument():
    # One toolset, two tools, two names. A model reads one block for all of them.
    instructions = _derive_instructions(
        {"a": _ordered_list_spec(), "b": _sorting_list_spec()},
        {},
    )

    assert "`ordering`" in instructions
    assert "`sorting`" in instructions


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("build", "args", "names"),
    [
        (_ordered_list_spec, {"ordering": ["cost"]}, ["b", "c", "a"]),
        (_ordered_list_spec, {"ordering": ["-cost"]}, ["a", "c", "b"]),
        (_sorting_list_spec, {"sorting": ["cost"]}, ["b", "c", "a"]),
    ],
    ids=["actually-orders-the-rows", "descends", "a-renamed-sort-still-orders-the-rows"],
)
def test_filter_owned_ordering_orders_the_rows(build, args, names):
    """The FilterSet advertises its sort, so the value must reach it and sort the rows."""
    user = User.objects.create(username="u")
    _three_widgets(user)
    result = _dispatch(build(), user, args)
    assert [w["name"] for w in _rows(result)] == names


@pytest.mark.django_db
def test_filter_owned_ordering_composes_with_the_other_filters():
    """``filter_data`` replaces ``params`` as the filter source, so other args must follow."""
    user = User.objects.create(username="u")
    _three_widgets(user)
    result = _dispatch(_ordered_list_spec(), user, {"min_price": "2", "ordering": ["cost"]})
    assert [w["name"] for w in _rows(result)] == ["c", "a"]


@pytest.mark.django_db
def test_filter_owned_tool_still_works_with_no_ordering_supplied():
    user = User.objects.create(username="u")
    _three_widgets(user)
    result = _dispatch(_ordered_list_spec(), user, {"min_price": "2"})
    assert sorted(w["name"] for w in _rows(result)) == ["a", "c"]


@pytest.mark.django_db
def test_an_ordering_outside_the_filters_choices_is_a_retry():
    """The FilterSet validates the sort, including a raw ORM path behind a public choice."""
    user = User.objects.create(username="u")
    _three_widgets(user)
    with pytest.raises(ModelRetry, match="ordering"):
        _dispatch(_ordered_list_spec(), user, {"ordering": "price"})


async def test_the_advertised_ordering_choices_are_the_filters_own_vocabulary():
    """The kernel reads an ``OrderingFilter`` as an array of its public choices."""
    toolset = SpecToolset({"list_widgets": _ordered_list_spec()})
    tools = await toolset.get_tools(None)
    props = tools["list_widgets"].tool_def.parameters_json_schema["properties"]
    assert props["ordering"]["type"] == "array"
    assert props["ordering"]["items"] == {
        "type": "string",
        "anyOf": [
            {"const": "cost", "title": "Cost"},
            {"const": "-cost", "title": "Cost (descending)"},
            {"const": "title", "title": "Title"},
            {"const": "-title", "title": "Title (descending)"},
        ],
    }
    assert props["ordering"]["maxItems"] == 1000


@pytest.mark.django_db
def test_a_sort_sent_as_one_string_is_handed_back_as_a_retry():
    """The schema advertises an array, so a string is the model's mistake to correct."""
    user = User.objects.create(username="u")
    _three_widgets(user)

    with pytest.raises(ModelRetry, match=r"^ordering: Expected an array\.$"):
        _dispatch(_ordered_list_spec(), user, {"ordering": "cost"})


def test_spec_owns_ordering_reads_the_reflected_schema():
    assert _spec_ordering_argument(_ordered_list_spec()) == "ordering"
    assert _spec_ordering_argument(_filtered_list_spec()) is None
    assert _spec_ordering_argument(list_spec()) is None
    # Only a list selector can contest the argument, so an ``ordering`` input field is no clash.
    assert _spec_ordering_argument(retrieve_spec()) is None
    assert _spec_ordering_argument(create_spec()) is None


def test_the_filters_vocabulary_survives_the_schema_merge_untouched():
    """Written under a filter's name, a schema entry would silently replace ``page``."""
    props = _input_schema(_ordered_list_spec())["properties"]
    choices = props["ordering"]["items"]["anyOf"]
    assert [choice["const"] for choice in choices] == ["cost", "-cost", "title", "-title"]
    assert {"page", "limit"} <= set(props)


async def test_the_ordering_instruction_is_emitted_for_a_filter_owned_tool():
    """The tool whose sort is a set of labelled options must be told to pick exactly one."""
    instructions = await SpecToolset({"list_widgets": _ordered_list_spec()}).get_instructions(None)
    assert "accept `ordering`" in instructions


@pytest.mark.django_db
def test_filter_owned_ordering_survives_pagination():
    """Sort and slice are owned by different layers, so the pair is asserted together."""
    user = User.objects.create(username="u")
    _three_widgets(user)
    result = _dispatch(_ordered_list_spec(), user, {"ordering": ["cost"], "page": 2, "limit": 2})
    assert [w["name"] for w in _rows(result)] == ["a"]
    assert (result["page"], result["totalPages"], result["hasNext"]) == (2, 2, False)


class _BogusTargetFilterSet(django_filters.FilterSet):
    """A sort whose public choice maps to something that is not a column."""

    ordering = django_filters.OrderingFilter(fields=(("nope", "broken"),))

    class Meta:
        model = Widget
        fields = []


@pytest.mark.django_db
def test_a_sort_target_that_is_not_a_column_raises_rather_than_retrying():
    """Pinned because it is easy to assume the opposite."""
    spec = SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_widgets,
        presenter=widget_presenter(),
        filter_set=_BogusTargetFilterSet,
        permissions=allow(),
    )
    user = User.objects.create(username="u")
    Widget.objects.create(name="a", price=1, owner=user)
    with pytest.raises(FieldError, match="Cannot resolve keyword 'nope'"):
        _dispatch(spec, user, {"ordering": ["broken"]})


# A selector declaring ``**kwargs`` is a catch-all for what the kernel hands
# it, so any transport arg left among the arguments would land in the
# callable's signature as an argument it never declared. That is why a
# filter-owned ``ordering`` is popped and routed through ``filter_data`` rather
# than simply left in place — and why ``page`` / ``limit`` must keep being popped.

_KWARGS_SEEN: list[dict[str, Any]] = []
_FILTER_DATA_SEEN: list[dict[str, Any]] = []


def list_widgets_greedy(user, **extras):
    """List widgets owned by the acting user, recording every extra argument."""
    _KWARGS_SEEN.append(dict(extras))
    return Widget.objects.filter(owner=user)


class _RecordingFilterSet(django_filters.FilterSet):
    """Records the filter data it was handed, so the split can be asserted on."""

    min_price = django_filters.NumberFilter(field_name="price", lookup_expr="gte")
    ordering = django_filters.OrderingFilter(fields=(("price", "cost"),))

    class Meta:
        model = Widget
        fields = []

    def __init__(self, data=None, queryset=None, *, request=None, prefix=None):
        _FILTER_DATA_SEEN.append(dict(data or {}))
        super().__init__(data=data, queryset=queryset, request=request, prefix=prefix)


@_FILTER_VALUES_REACH_KWARGS
@pytest.mark.django_db
def test_the_transport_args_never_reach_a_kwargs_selector():
    """``page`` / ``limit`` / ``ordering`` are the transport's, whoever consumes them."""
    _KWARGS_SEEN.clear()
    _FILTER_DATA_SEEN.clear()
    user = User.objects.create(username="u")
    _three_widgets(user)
    spec = SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_widgets_greedy,
        presenter=widget_presenter(),
        filter_set=_RecordingFilterSet,
        permissions=allow(),
    )
    result = _dispatch(spec, user, {"page": 1, "limit": 2, "ordering": ["cost"], "min_price": "1"})
    assert [w["name"] for w in _rows(result)] == ["b", "c"]

    seen = _KWARGS_SEEN[-1]
    assert "page" not in seen
    assert "limit" not in seen
    assert "ordering" not in seen
    assert seen["min_price"] == "1"

    assert _FILTER_DATA_SEEN[-1] == {"min_price": "1", "ordering": "cost"}


# A list selector with no ``filter_set`` whose *callable* declares ``ordering``:
# the schema advertises it for the same reason, so the same invariant applies —
# but the argument is the selector's own, and has to stay in ``params`` where the
# callable is given it rather than being routed to a FilterSet that isn't there.


def list_widgets_sorted(user, ordering: str = "name"):
    """List widgets owned by the acting user, in a given order."""
    return Widget.objects.filter(owner=user).order_by(ordering)


@pytest.mark.django_db
def test_an_ordering_the_selector_itself_declares_reaches_the_selector():
    user = User.objects.create(username="u")
    _three_widgets(user)
    spec = SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_widgets_sorted,
        reads=Parameters.of(Parameter("ordering", "string", default="name")),
        presenter=widget_presenter(),
        permissions=allow(),
    )
    assert _spec_ordering_argument(spec) == "ordering"
    result = _dispatch(spec, user, {"ordering": "price"})
    assert [w["name"] for w in _rows(result)] == ["b", "c", "a"]


# A selector whose ``ordering`` parameter the toolset fills is the opposite case:
# the schema leaves the name out, so the instructions and the call have to agree
# that the tool takes no sort from the model.


def list_widgets_in_house_order(user, ordering):
    """List widgets owned by the acting user, in the order the deployment sets."""
    return Widget.objects.filter(owner=user).order_by(ordering)


class _HouseOrder(TypedDict):
    ordering: str


def _house_order(view) -> _HouseOrder:
    """The deployment's sort, which the caller has no say in."""
    return _HouseOrder(ordering="price")


def _house_ordered_spec(**kwargs: Any) -> SelectorSpec:
    return SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_widgets_in_house_order,
        presenter=widget_presenter(),
        permissions=allow(),
        **kwargs,
    )


#: The two channels that fill ``ordering`` for the selector, so the model is not
#: asked for it: a typed ``kwargs=`` provider on the spec, and a registered seed.
_HOUSE_ORDER_FILLERS = pytest.mark.parametrize(
    ("spec_kwargs", "toolset_kwargs"),
    [
        ({"kwargs": _house_order}, {}),
        ({}, {"pool_seeds": DEFAULT_POOL_SEEDS.extend(ordering=lambda: "price")}),
    ],
    ids=["provider", "pool-seed"],
)


@_HOUSE_ORDER_FILLERS
@pytest.mark.django_db(transaction=True)
async def test_an_ordering_the_toolset_fills_is_refused_from_the_model(spec_kwargs, toolset_kwargs):
    """The schema drops a name the toolset fills, and the call refuses a sort sent anyway."""
    user = await User.objects.acreate(username="u")
    await sync_to_async(_three_widgets)(user)
    toolset = SpecToolset({"list_widgets": _house_ordered_spec(**spec_kwargs)}, **toolset_kwargs)

    assert "ordering" not in _input_schema_of(toolset, "list_widgets")["properties"]
    with pytest.raises(ModelRetry) as refused:
        await _call(toolset, "list_widgets", user, {"ordering": "-price"})
    result = await _call(toolset, "list_widgets", user)

    assert str(refused.value) == "This tool does not accept an `ordering` argument; omit it."
    assert [w["name"] for w in _rows(result)] == ["b", "c", "a"]


@_HOUSE_ORDER_FILLERS
@pytest.mark.parametrize("books_open", [True, False], ids=["every-tool", "a-tool-left-out"])
async def test_an_ordering_the_toolset_fills_gets_no_usage_line(
    spec_kwargs, toolset_kwargs, books_open
):
    """No line teaches a sort argument no tool lets the model send."""
    toolset = SpecToolset(
        {
            "list_widgets": _house_ordered_spec(**spec_kwargs),
            "approve": _conditioned_spec(lambda: books_open),
        },
        **toolset_kwargs,
    )

    instructions = await toolset.get_instructions(ctx_for(User(username="u")))

    assert (_DEFAULTS.unavailable_heading in instructions) is not books_open
    assert _pagination_line(DEFAULT_PAGE_SIZE) in instructions
    assert _ordering_line(["ordering"]) not in instructions


# ----- progress: accepted and forwarded, never constructed -----


class _RecordingProgress:
    """Stands in for whatever is driving the agent — an SSE frame, a task record."""

    def __init__(self):
        self.reports = []

    def __call__(self, progress, *, total=None, message=None, meta=None):
        self.reports.append((progress, total, message, meta))


def reporting_service(data, user, progress):
    """Creates a widget, reporting as it goes."""
    progress(1, total=1, message="Creating")
    return Widget.objects.create(owner=user, **data)


def reporting_spec():
    return ServiceSpec(
        service=reporting_service,
        validator=widget_input(),
        presenter=widget_presenter(),
        permissions=allow(),
    )


@pytest.mark.django_db(transaction=True)
async def test_a_reporter_on_the_deps_reaches_the_service():
    """The caller supplies the sink; the toolset only carries it."""
    user = await sync_to_async(User.objects.create)(username="u")
    sink = _RecordingProgress()
    toolset = SpecToolset({"create_widget": reporting_spec()})
    tools = await toolset.get_tools(None)

    await toolset.call_tool(
        "create_widget",
        {"name": "a", "price": 1},
        ctx_for(deps=AgentDeps(user=user, progress=sink)),
        tools["create_widget"],
    )
    assert sink.reports == [(1, 1, "Creating", None)]


@pytest.mark.django_db(transaction=True)
async def test_the_same_service_runs_with_no_reporter_supplied():
    """``None`` is the ordinary case: the kernel substitutes its no-op."""
    user = await sync_to_async(User.objects.create)(username="u")
    toolset = SpecToolset({"create_widget": reporting_spec()})
    tools = await toolset.get_tools(None)

    result = await toolset.call_tool(
        "create_widget", {"name": "a", "price": 1}, ctx_for(user), tools["create_widget"]
    )
    assert result["name"] == "a"


async def test_a_deps_type_without_a_progress_field_is_fine():
    """A project's own deps class may carry no ``progress``; a missing sink is normal."""
    assert _default_get_progress(SimpleNamespace(deps=SimpleNamespace(user=None))) is None
    assert _default_get_progress(SimpleNamespace()) is None


# ----- the logger -----


@pytest.mark.django_db(transaction=True)
async def test_a_call_is_timed_at_debug(caplog):
    user = await sync_to_async(User.objects.create)(username="u")
    toolset = SpecToolset({"list_widgets": list_spec()})
    tools = await toolset.get_tools(None)

    with caplog.at_level(logging.DEBUG, logger="django_spec_agents.spec_tools"):
        await toolset.call_tool("list_widgets", {}, ctx_for(user), tools["list_widgets"])

    assert "Tool 'list_widgets' on toolset 'spec-tools' took" in caplog.text


@pytest.mark.django_db(transaction=True)
async def test_a_denial_is_logged_at_warning_and_still_raises(caplog):
    """The one failure with no other trace."""
    user = await sync_to_async(User.objects.create)(username="u")
    toolset = SpecToolset({"list_widgets": list_spec(permissions=[DenyAll()])})
    tools = await toolset.get_tools(None)

    with (
        caplog.at_level(logging.WARNING, logger="django_spec_agents.spec_tools"),
        pytest.raises(NotPermitted),
    ):
        await toolset.call_tool("list_widgets", {}, ctx_for(user), tools["list_widgets"])

    assert "Permission denied calling tool 'list_widgets'" in caplog.text


@pytest.mark.django_db(transaction=True)
async def test_a_refusal_the_service_raises_is_logged_as_a_denial(caplog):
    """A bare ``NotPermitted`` raised by the service is answered by the denial arm too."""

    def refuse(user):
        """Refuse."""
        raise NotPermitted("not yours")

    user = await sync_to_async(User.objects.create)(username="u")
    toolset = SpecToolset(
        {
            "refuse": django_service_specs.ServiceSpec(
                service=refuse, permissions=[django_service_specs.Unrestricted()], atomic=False
            )
        }
    )

    with (
        caplog.at_level(logging.WARNING, logger="django_spec_agents.spec_tools"),
        pytest.raises(NotPermitted),
    ):
        await toolset.call_tool("refuse", {}, ctx_for(user), None)

    assert "Permission denied calling tool 'refuse'" in caplog.text


@pytest.mark.django_db(transaction=True)
async def test_djangos_permission_denied_is_logged_as_a_denial(caplog):
    """A bare Django ``PermissionDenied`` is the same refusal, logged naming the tool."""

    def refuse(user):
        """Refuse."""
        raise DjangoPermissionDenied("not yours")

    user = await sync_to_async(User.objects.create)(username="u")
    toolset = SpecToolset(
        {
            "refuse": django_service_specs.ServiceSpec(
                service=refuse, permissions=[django_service_specs.Unrestricted()], atomic=False
            )
        }
    )

    with (
        caplog.at_level(logging.WARNING, logger="django_spec_agents.spec_tools"),
        pytest.raises(DjangoPermissionDenied),
    ):
        await toolset.call_tool("refuse", {}, ctx_for(user), None)

    assert "Permission denied calling tool 'refuse'" in caplog.text


@pytest.mark.django_db(transaction=True)
async def test_a_deactivated_principal_is_logged_as_a_denial(caplog):
    """A deactivated principal gets ``PrincipalUnavailable``, still a denial."""
    user = await sync_to_async(User.objects.create)(username="u", is_active=False)
    toolset = SpecToolset({"list_widgets": list_spec()})

    with (
        caplog.at_level(logging.WARNING, logger="django_spec_agents.spec_tools"),
        pytest.raises(PrincipalUnavailable),
    ):
        await toolset.call_tool("list_widgets", {}, ctx_for(user), None)

    assert "Permission denied calling tool 'list_widgets'" in caplog.text


# ----- outbound bounds -----


@pytest.mark.django_db(transaction=True)
async def test_an_over_budget_result_fails_rather_than_truncating():
    """A partial payload looks complete."""
    user = await sync_to_async(User.objects.create)(username="u")
    for n in range(5):
        await sync_to_async(Widget.objects.create)(name=f"w{n}", price=n, owner=user)

    toolset = SpecToolset({"list_widgets": list_spec()}, max_result_bytes=20)
    tools = await toolset.get_tools(None)
    with pytest.raises(ToolFailed) as caught:
        await toolset.call_tool("list_widgets", {}, ctx_for(user), tools["list_widgets"])

    assert "over the 20 byte ceiling" in caught.value.message
    assert "not truncated" in caught.value.message


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "ceilings",
    [
        {"max_result_bytes": 100_000},
        # Absent and ``None`` are different answers: one inherits, one opts out.
        {"max_result_bytes": 1, "tool_max_result_bytes": {"list_widgets": None}},
    ],
    ids=[
        "a-result-inside-the-ceiling-passes-through-untouched",
        "a-per-tool-ceiling-of-none-means-no-ceiling-for-that-tool",
    ],
)
async def test_a_result_the_ceiling_admits_passes_through_untouched(ceilings):
    user = await sync_to_async(User.objects.create)(username="u")
    await sync_to_async(Widget.objects.create)(name="a", price=1, owner=user)

    toolset = SpecToolset({"list_widgets": list_spec()}, **ceilings)
    tools = await toolset.get_tools(None)
    result = await toolset.call_tool("list_widgets", {}, ctx_for(user), tools["list_widgets"])

    assert [w["name"] for w in _rows(result)] == ["a"]


def test_a_per_tool_ceiling_for_an_unknown_tool_is_a_typo():
    with pytest.raises(ValueError, match="unknown tool 'nope'"):
        SpecToolset({"list_widgets": list_spec()}, tool_max_result_bytes={"nope": 10})


async def test_the_page_ceiling_is_advertised_on_limit():
    toolset = SpecToolset({"list_widgets": list_spec()}, max_page_size=50)
    tools = await toolset.get_tools(None)
    props = tools["list_widgets"].tool_def.parameters_json_schema["properties"]
    assert props["limit"]["maximum"] == 50


@pytest.mark.django_db
def test_the_page_ceiling_is_clamped_as_well_as_advertised():
    """Advertising alone is a hint that nothing obliges a model to honour."""
    user = User.objects.create(username="u")
    for n in range(5):
        Widget.objects.create(name=f"w{n}", price=n, owner=user)

    result = _dispatch(list_spec(), user, {"limit": 1000}, max_page_size=2)
    assert len(_rows(result)) == 2


@pytest.mark.django_db
def test_an_omitted_limit_becomes_the_ceiling():
    """The unbounded read hurts most, and a model produces it by not thinking about pages."""
    user = User.objects.create(username="u")
    for n in range(5):
        Widget.objects.create(name=f"w{n}", price=n, owner=user)

    assert len(_rows(_dispatch(list_spec(), user, {}, max_page_size=3))) == 3
    page = _dispatch(list_spec(), user, {})
    assert len(_rows(page)) == 5
    assert page["hasNext"] is False


async def test_a_call_past_its_deadline_answers_instead_of_hanging():
    async def _slow():
        await asyncio.sleep(1)

    with pytest.raises(ToolFailed) as caught:
        await _with_deadline(_slow(), 0.01, label="list_widgets")
    assert "longer than the 0.01s limit" in caught.value.message


async def test_no_deadline_awaits_normally():
    async def _quick():
        return "done"

    assert await _with_deadline(_quick(), None, label="x") == "done"


# --- the overridable middle --------------------------------------------------
#
# Everything between argument intake and result rendering is a seam, so a
# subclass need not replace ``call_tool`` wholesale. These cover the two seams
# and the two knobs that ride on them.


class _Boom(Exception):
    """Stands in for an exception this package knows nothing about."""


def _raising(exc: BaseException) -> Any:
    """A service taking the acting user that raises ``exc``."""

    def boom(user):
        """Boom."""
        raise exc

    return boom


def _retry_instead(exc: BaseException) -> Any:
    """An ``exception_map`` handler that asks the model to retry."""
    raise ModelRetry(str(exc))


def test_an_unmapped_exception_still_aborts_the_run():
    """By default a genuine bug must not become a tool result."""
    boom = _raising(_Boom("kaboom"))

    with pytest.raises(_Boom):
        _dispatch(ServiceSpec(service=boom, atomic=False), object())


def test_exception_map_turns_an_unknown_exception_into_a_result():
    """The motivating case: Django's ``ValidationError``, not the kernel's."""
    boom = _raising(DjangoValidationError("that name is taken"))

    result = _dispatch(
        ServiceSpec(service=boom, atomic=False),
        object(),
        toolset_kwargs={"exception_map": {DjangoValidationError: lambda exc: {"error": str(exc)}}},
    )
    assert "that name is taken" in result["error"]


def test_a_handler_may_retry_instead_of_returning():
    """Both outcomes of the model loop are reachable from one map."""
    boom = _raising(_Boom("try again"))

    with pytest.raises(ModelRetry, match="try again"):
        _dispatch(
            ServiceSpec(service=boom, atomic=False),
            object(),
            toolset_kwargs={"exception_map": {_Boom: _retry_instead}},
        )


def test_a_base_class_registration_catches_a_subclass():
    """Registering ``Exception`` should not require enumerating every subclass."""
    boom = _raising(_Boom("anything"))

    result = _dispatch(
        ServiceSpec(service=boom, atomic=False),
        object(),
        toolset_kwargs={"exception_map": {Exception: lambda exc: {"error": "caught"}}},
    )
    assert result == {"error": "caught"}


def test_the_most_specific_registration_wins():
    """MRO order, so a narrow handler is not shadowed by a broad one."""
    boom = _raising(_Boom("x"))

    result = _dispatch(
        ServiceSpec(service=boom, atomic=False),
        object(),
        toolset_kwargs={
            "exception_map": {
                Exception: lambda exc: {"error": "broad"},
                _Boom: lambda exc: {"error": "narrow"},
            }
        },
    )
    assert result == {"error": "narrow"}


def test_the_map_overrides_a_built_in_arm():
    """Deliberate: the built-ins are this package's guess, not a law."""
    boom = _raising(ServiceError("nope"))

    with pytest.raises(ModelRetry):
        _dispatch(
            ServiceSpec(service=boom, atomic=False),
            object(),
            toolset_kwargs={"exception_map": {ServiceError: _retry_instead}},
        )


def _tenant_peek() -> tuple[Any, dict[str, Any]]:
    """A service recording, in the dict beside it, the tenant header it was shown."""
    seen: dict[str, Any] = {}

    def peek(user, request=None, **_):
        """Peek."""
        seen["tenant"] = request.META.get("HTTP_X_TENANT")
        return {"ok": True}

    return peek, seen


@pytest.mark.django_db
def test_no_request_arrives_unless_the_run_put_one():
    """The honest default -- a request nothing declared is one nobody can audit."""
    seen: dict[str, Any] = {}

    def peek(user, request=None, **_):
        """Peek."""
        seen["request"] = request
        return {"ok": True}

    _dispatch(ServiceSpec(service=peek, atomic=False), object())
    assert seen == {"request": None}


@pytest.mark.django_db
def test_a_provider_declaring_view_is_handed_none_on_a_run_with_no_view():
    """No view exists off HTTP: a ``kwargs=`` provider gets ``None`` for ``view``."""
    seen: dict[str, Any] = {}

    def scope(view):
        seen["view"] = view
        return {}

    _dispatch(list_spec(kwargs=scope), User.objects.create(username="u"))

    assert seen == {"view": None}


@pytest.mark.django_db
def test_a_request_call_seeds_returns_stands_over_the_one_the_run_put():
    """A subclass reaches dispatch with the run's typed deps in hand."""
    peek, seen = _tenant_peek()

    class TenantToolset(SpecToolset):
        def call_seeds(self, *, ctx, tool_name):
            request = RequestFactory().get("/", HTTP_X_TENANT=ctx.deps.user)
            return {"request": lambda: request}

    spec = ServiceSpec(service=peek, atomic=False, permissions=allow())
    toolset = TenantToolset({"t": spec})
    deps = AgentDeps(user="gamma-ltd")
    deps.call_context = {"request": RequestFactory().get("/", HTTP_X_TENANT="arrived")}
    toolset._call_spec(spec, "gamma-ltd", {}, ctx=ctx_for(deps=deps), label="t")

    assert seen["tenant"] == "gamma-ltd"


def test_translate_exception_is_overridable_without_a_map():
    """The method is the seam; ``exception_map`` is the declarative shortcut."""
    boom = _raising(_Boom("subclassed"))

    class Translating(SpecToolset):
        def translate_exception(self, exc, *, ctx):
            if isinstance(exc, _Boom):
                return lambda e: {"error": f"handled {e}"}
            return super().translate_exception(exc, ctx=ctx)

    spec = ServiceSpec(service=boom, atomic=False, permissions=allow())
    toolset = Translating({"t": spec})
    ctx = ctx_for(object())

    assert toolset._call_spec(spec, object(), {}, ctx=ctx, label="t") == {
        "error": "handled subclassed"
    }


# --- the composing caller's view of a built toolset --------------------------


def test_specs_is_readable_without_a_run():
    """``get_tools`` is async and needs a ``RunContext``, so it cannot answer here."""
    a, b = list_spec(), retrieve_spec()
    toolset = SpecToolset({"list": a, "get": b})

    assert dict(toolset.specs) == {"list": a, "get": b}


def test_specs_cannot_be_written_through():
    """A tool added here would have skipped every check the constructor ran."""
    toolset = SpecToolset({"list": list_spec()})

    with pytest.raises(TypeError):
        toolset.specs["sneaky"] = list_spec()  # ty: ignore[unsupported-operation]


def test_specs_reflects_a_registry_source_resolved():
    """A ``SpecRegistry`` in, a plain mapping out — the same normalisation."""
    registry = SpecRegistry()
    registry.register("list", list_spec())

    assert set(SpecToolset(registry).specs) == {"list"}


# --- agent audience -----------------------------------------------------------


class _AgentWidget(RowPresenter):
    """A widget marked for an agent: its handle, its label, a hidden price, a spoken status."""

    def __init__(self) -> None:
        super().__init__(
            OutputField("id", "integer", marking=FieldMarking.handle("Widget handle.")),
            OutputField("name", "string", marking=FieldMarking.label()),
            OutputField("price", "integer", marking=FieldMarking.hidden()),
            OutputField(
                "status",
                "string",
                choices=(("IN_STOCK", "In stock"), ("BACKORDER", "On backorder")),
            ),
        )

    def present(self, value: Any) -> Any:
        return {"id": value.id, "name": value.name, "price": value.price, "status": "IN_STOCK"}


def agent_list_spec(**kwargs):
    kwargs.setdefault("permissions", allow())
    return SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_widgets,
        presenter=_AgentWidget(),
        **kwargs,
    )


@pytest.mark.django_db
def test_hidden_fields_leave_the_payload_and_choices_are_spoken():
    """The payload half of the projection; the schema half is asserted below."""
    user = User.objects.create(username="u")
    Widget.objects.create(name="Sprocket", price=100, owner=user)

    rows = _dispatch(agent_list_spec(), user)

    assert _rows(rows) == [{"id": 1, "name": "Sprocket", "status": "In stock"}]


# ``transaction=True`` because neither write below can be rolled back.
@pytest.mark.django_db(transaction=True)
async def test_call_tool_uses_the_projection_built_at_construction():
    user = await User.objects.acreate(username="u2")
    widget = await Widget.objects.acreate(name="Sprocket", price=100, owner=user)
    toolset = SpecToolset({"widgets": agent_list_spec()})

    rows = await toolset.call_tool("widgets", {}, ctx_for(user), None)

    assert _rows(rows) == [{"id": widget.pk, "name": "Sprocket", "status": "In stock"}]


async def test_handle_instruction_present_only_when_a_tool_has_a_handle():
    with_handle = await SpecToolset({"widgets": agent_list_spec()}).get_instructions(None)
    assert _DEFAULTS.handles in with_handle

    # An unmarked presenter teaches the model nothing about handles.
    without = await SpecToolset({"list": list_spec()}).get_instructions(None)
    assert _DEFAULTS.handles not in without


def _declaring(*reads: Parameter, shapes: Sequence[Parameter] = ()):
    """A kernel spec declaring ``reads`` and ``shapes``, for the reads the call fills."""
    return django_service_specs.SelectorSpec(
        kind=SelectorKind.RETRIEVE,
        selector=lambda **_: None,
        reads=Parameters.of(*reads),
        shapes=Parameters.of(*shapes),
        permissions=[django_service_specs.Unrestricted()],
    )


@pytest.mark.parametrize("no_default", [None, UNSET])
def test_a_read_or_shape_declaring_no_default_contributes_nothing(no_default: Any) -> None:
    """Both sentinels for no default, ``None`` and ``UNSET``, read as absent."""
    args: dict[str, Any] = {}
    _fill_reads(
        _declaring(
            Parameter("expand", "string", default=no_default),
            shapes=[Parameter("fields", "string", default=no_default)],
        ),
        args,
    )
    assert args == {}


def test_a_real_default_still_reaches_the_spec() -> None:
    """The other half of the predicate: a declared default is still applied."""
    args: dict[str, Any] = {}
    _fill_reads(
        _declaring(
            Parameter("expand", "string", default="all"),
            shapes=[Parameter("fields", "string", default="id")],
        ),
        args,
    )
    assert args == {"fields": "id", "expand": "all"}


def test_a_reflected_read_s_default_is_its_callable_s_to_apply() -> None:
    """A ``reflected`` read carries the callable's own default, which the callable applies."""
    args: dict[str, Any] = {}
    _fill_reads(_declaring(Parameter("region", "string", default="all", reflected=True)), args)
    assert args == {}


# --- what a permission check sees off HTTP -----------------------------------
#
# A permission check may ask for the call's context and for the ``request`` and
# ``view`` seeds each call registers. These pin what this package puts in each,
# because a permission check is the only thing standing between a model and a
# spec, and many of them are authored against the HTTP transport.


def _do_it(user):
    """Do it."""
    return {"ok": True}


async def _call(toolset: SpecToolset, name: str, user: Any, args: dict[str, Any] | None = None):
    tools = await toolset.get_tools(ctx_for(user))
    return await toolset.call_tool(name, dict(args or {}), ctx_for(user), tools[name])


@pytest.mark.django_db
async def test_the_tool_name_is_the_action_a_permission_check_reads():
    """The call's ``action`` is the tool name, never ``None``."""
    toolset = SpecToolset(
        {
            "suspend_account": ServiceSpec(
                service=_do_it, atomic=False, permissions=[RecordingPermission()]
            )
        },
        descriptions={"suspend_account": "Suspend an account."},
    )
    RecordingPermission.seen = {}

    await _call(toolset, "suspend_account", object())

    assert RecordingPermission.seen["action"] == "suspend_account"


@pytest.mark.django_db
async def test_a_view_call_seeds_returns_carries_the_action_a_permission_class_reads():
    """The tool's name is the call's ``action``, and a view is how to say another."""

    class RetrieveShaped(SpecToolset):
        def call_seeds(self, *, ctx, tool_name):
            view = SimpleNamespace(action="retrieve", kwargs={})
            return {"view": lambda: view}

    toolset = RetrieveShaped(
        {
            "get_thing": ServiceSpec(
                service=_do_it, atomic=False, permissions=[RecordingPermission()]
            )
        },
        descriptions={"get_thing": "Get a thing."},
    )
    RecordingPermission.seen = {}

    await _call(toolset, "get_thing", object())

    assert RecordingPermission.seen["view"].action == "retrieve"
    assert RecordingPermission.seen["action"] == "get_thing"


_PROJECT_PK = Parameter("project_pk", "integer", required=True)


def _in_project(user, project_pk):
    """Do it in a project."""
    return {"ok": True}


@pytest.mark.django_db
async def test_a_view_call_seeds_returns_is_handed_over_with_no_route_read_filled():
    """What the moving guide tells a project whose permission reads a route capture."""
    carried = SimpleNamespace(action="do_it", kwargs={})

    class Carrying(SpecToolset):
        def call_seeds(self, *, ctx, tool_name):
            return {"view": lambda: carried}

    def spec():
        return ServiceSpec(
            service=_in_project,
            atomic=False,
            reads=Parameters.of(_PROJECT_PK),
            permissions=[RecordingPermission()],
        )

    for toolset_class, view in ((Carrying, carried), (SpecToolset, None)):
        toolset = toolset_class({"do_it": spec()}, descriptions={"do_it": "Do it."})
        RecordingPermission.seen = {}

        await _call(toolset, "do_it", object(), {"project_pk": 7})

        assert RecordingPermission.seen["view"] is view
        assert RecordingPermission.seen["arguments"] == {"project_pk": 7}
    assert carried.kwargs == {}


async def _call_arrived(
    toolset: SpecToolset,
    name: str,
    arrived: Any,
    args: dict[str, Any] | None = None,
    *,
    user: Any = None,
) -> Any:
    """Call ``name`` for ``user`` in a run that arrived on ``arrived``, in its ``call_context``."""
    deps = AgentDeps(user=object() if user is None else user)
    deps.call_context = {"request": arrived}
    ctx = ctx_for(deps=deps)
    tools = await toolset.get_tools(ctx)
    return await toolset.call_tool(name, dict(args or {}), ctx, tools[name])


# --- one arrived-on request, several calls -----------------------------------


# What each render of ``_SlowEcho`` read: the call's ``fields`` before and after
# its pause, and the arrived-on request's marker.
_ECHOED: list[tuple[Any, Any, Any]] = []


class _SlowEcho(Presenter):
    """Reads its call's ``fields`` shape either side of a pause."""

    def output(self) -> Output:
        return Output((OutputField("name", "string"), OutputField("fields", "string")))

    def present(self, value: Any) -> Any:
        raise AssertionError("rendered without its call's context")

    def presented(self, value: Any, context: Any) -> Any:
        before = context.shapes.get("fields")
        time.sleep(0.2)
        request = context.call_context["request"]
        _ECHOED.append((before, context.shapes.get("fields"), request.META.get("HTTP_X_MARKER")))
        return {"name": value.name, "fields": context.shapes.get("fields")}


@pytest.mark.django_db(transaction=True)
async def test_two_concurrent_calls_built_around_one_arrived_on_request_each_read_their_own():
    """One request object serves the whole run, and every call of the run is built around it."""
    _ECHOED.clear()
    user = await sync_to_async(User.objects.create)(username="u")
    await sync_to_async(Widget.objects.create)(name="a", price=1, owner=user)
    spec = SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_widgets,
        presenter=_SlowEcho(),
        permissions=allow(),
    )
    toolset = SpecToolset(
        shaping({"w": spec}, {"w": [Parameter("fields", "string")]}),
        descriptions={"w": "W."},
        thread_sensitive=False,
    )
    arrived = RequestFactory().get("/agent/?fields=original", HTTP_X_MARKER="arrived")
    deps = AgentDeps(user=user)
    deps.call_context = {"request": arrived}
    ctx = ctx_for(deps=deps)
    tools = await toolset.get_tools(ctx)

    one, two = await asyncio.gather(
        toolset.call_tool("w", {"fields": "id"}, ctx, tools["w"]),
        toolset.call_tool("w", {"fields": "name"}, ctx, tools["w"]),
    )

    assert one["items"] == [{"name": "a", "fields": "id"}]
    assert two["items"] == [{"name": "a", "fields": "name"}]
    assert sorted(_ECHOED) == [("id", "id", "arrived"), ("name", "name", "arrived")]
    assert arrived.GET.dict() == {"fields": "original"}


@pytest.mark.django_db
async def test_a_dispatch_leaves_the_arrived_on_request_untouched():
    """The second half: the request the run arrived on survives the call unchanged."""
    ambient = RequestFactory().get("/agent/?fields=original", HTTP_X_MARKER="arrived")
    ambient.user = "the-operator"
    toolset = SpecToolset(
        reading(
            {
                "do_it": ServiceSpec(
                    service=_do_it, atomic=False, permissions=[RecordingPermission()]
                )
            },
            {"do_it": [Parameter("fields", "string")]},
        ),
        descriptions={"do_it": "Do it."},
    )
    RecordingPermission.seen = {}

    await _call_arrived(toolset, "do_it", ambient, {"fields": "id,name"})

    assert RecordingPermission.seen["marker"] == "arrived"
    assert ambient.method == "GET"
    assert ambient.GET.dict() == {"fields": "original"}
    assert ambient.user == "the-operator"


# --- the tool catalog --------------------------------------------------------


async def test_every_tool_is_listed_by_default():
    """The documented posture: listing discloses, ``permission_classes`` gate."""
    toolset = SpecToolset({"list_widgets": list_spec(), "make": create_spec()})

    tools = await toolset.get_tools(ctx_for(object()))

    assert sorted(tools) == ["list_widgets", "make"]


async def test_is_tool_listed_can_narrow_the_catalog_for_one_run():
    """The seam for a deployment that does want a per-run catalog."""

    class StaffScoped(SpecToolset):
        async def is_tool_listed(self, name, ctx):
            return name != "make" or ctx.deps.user == "staff"

    toolset = StaffScoped({"list_widgets": list_spec(), "make": create_spec()})

    assert sorted(await toolset.get_tools(ctx_for("staff"))) == ["list_widgets", "make"]
    assert sorted(await toolset.get_tools(ctx_for("visitor"))) == ["list_widgets"]


async def test_a_hidden_tool_is_still_callable_and_still_gated():
    """Hiding is a disclosure decision, never an authorization one."""

    class HideEverything(SpecToolset):
        async def is_tool_listed(self, name, ctx):
            return False

    toolset = HideEverything({"denied": create_spec(permissions=[DenyAll()])})

    assert await toolset.get_tools(ctx_for(object())) == {}
    with pytest.raises(NotPermitted):
        await toolset.call_tool("denied", {"name": "x", "price": 1}, ctx_for(object()), None)


# --- operations unavailable right now ----------------------------------------
#
# A ``ServiceSpec`` affordance answered without a row -- a callable ``when`` --
# is asked each step, and a tool it refuses is left out of the catalog and named,
# with its reason, in the instructions. Unlike a permission, it reads only seeds,
# so it cannot hide a tool the caller could have invoked with other arguments.


def _approve() -> dict[str, bool]:
    """Approve the pending invoices."""
    return {"approved": True}


def _conditioned_spec(
    when: Any, *, code: str = "books_closed", reason: str = "The books are closed."
) -> ServiceSpec:
    """A service whose one affordance is ``when``."""
    return ServiceSpec(
        service=_approve,
        atomic=False,
        affordances=[Affordance(code=code, reason=reason, when=when)],
        permissions=allow(),
    )


def _books_open_for(user: Any) -> bool:
    """Met for the user named ``open`` and nobody else: an answer that differs by caller."""
    return user.username == "open"


@pytest.mark.parametrize(("username", "listed"), [("open", True), ("closed", False)])
async def test_an_operation_is_listed_only_while_its_condition_is_met(username, listed):
    toolset = SpecToolset(
        {"list_widgets": list_spec(), "approve": _conditioned_spec(_books_open_for)}
    )

    tools = await toolset.get_tools(ctx_for(User(username=username)))

    assert ("approve" in tools) is listed
    assert "list_widgets" in tools


async def test_a_row_condition_is_listed_without_a_hop_or_a_pool(monkeypatch):
    """There is no row at listing time, so a condition on one is never asked here."""

    def no_hop(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("listing a row-conditioned spec hopped to a thread")

    def no_pool(**_: Any) -> Any:
        raise AssertionError("listing a row-conditioned spec built a pool")

    toolset = SpecToolset(
        {
            "cancel": _conditioned_spec(Q(pk__in=[])),
            "orders": _order_selector_spec(SelectorKind.RETRIEVE),
        },
        descriptions={"cancel": "Cancel an order."},
    )
    monkeypatch.setattr(spec_toolset, "sync_to_async", no_hop)
    monkeypatch.setattr(spec_toolset, "seeded_pool", no_pool)

    tools = await toolset.get_tools(ctx_for(User(username="u")))

    assert sorted(tools) == ["cancel", "orders"]


async def test_a_toolset_declaring_no_condition_builds_no_pool_and_reads_no_user(monkeypatch):
    """The ordinary toolset pays nothing: no pool, no hop, not even the user."""
    built: list[Any] = []
    monkeypatch.setattr(spec_toolset, "seeded_pool", lambda **kw: built.append(kw))

    def no_user(ctx: Any) -> Any:
        raise AssertionError("a toolset with no condition read the user to list")

    toolset = SpecToolset({"list_widgets": list_spec(), "make": create_spec()}, get_user=no_user)

    tools = await toolset.get_tools(ctx_for())
    instructions = await toolset.get_instructions(ctx_for())

    assert sorted(tools) == ["list_widgets", "make"]
    assert instructions == toolset._derived_instructions
    assert built == []


async def test_is_tool_listed_returning_true_does_not_relist_an_unavailable_tool():
    """The omission is outside the overridable default, so an override cannot switch it off."""

    class ListEverything(SpecToolset):
        async def is_tool_listed(self, name, ctx):
            return True

    toolset = ListEverything(
        {"list_widgets": list_spec(), "approve": _conditioned_spec(lambda: False)}
    )

    assert sorted(await toolset.get_tools(ctx_for(User(username="u")))) == ["list_widgets"]


async def test_the_instructions_name_an_unavailable_operation_and_describe_only_the_rest():
    """Everything describing the omitted tool goes, and its name and reason arrive."""
    books = {"open": False}
    toolset = SpecToolset(
        shaping(
            {
                "list_widgets": list_spec(),
                "approve": _conditioned_spec(lambda: books["open"]),
                "ping": _conditioned_spec(lambda: True, code="always", reason="Never shown."),
            },
            {"approve": [Parameter("fields", "string")]},
        )
    )
    ctx = ctx_for(User(username="u"))

    closed = await toolset.get_instructions(ctx)

    assert closed.endswith(f"{_DEFAULTS.unavailable_heading}\n  - `approve`: The books are closed.")
    assert "`fields`" not in closed
    assert _pagination_line(DEFAULT_PAGE_SIZE) in closed
    assert "ping" not in closed
    assert "Never shown." not in closed
    assert "list_widgets" not in closed

    books["open"] = True
    opened = await toolset.get_instructions(ctx)

    assert opened == toolset._derived_instructions
    assert "`fields`" in opened
    assert _DEFAULTS.unavailable_heading not in opened


async def test_an_instructions_override_is_kept_and_the_unavailable_operations_follow_it():
    """The override replaces the conventions, not the per-step availability."""
    books = {"open": False}
    toolset = SpecToolset(
        {"approve": _conditioned_spec(lambda: books["open"])}, instructions="House rules."
    )
    ctx = ctx_for(User(username="u"))

    assert await toolset.get_instructions(ctx) == (
        f"House rules.\n{_DEFAULTS.unavailable_heading}\n  - `approve`: The books are closed."
    )
    books["open"] = True
    assert await toolset.get_instructions(ctx) == "House rules."


async def test_one_toolset_serving_two_users_gives_each_their_own_answer():
    """A shared instance, interleaved runs, conditions answering differently."""
    toolset = SpecToolset(
        {"list_widgets": list_spec(), "approve": _conditioned_spec(_books_open_for)}
    )
    opened = ctx_for(User(username="open"))
    closed = ctx_for(User(username="closed"))

    for _ in range(2):
        assert "approve" in await toolset.get_tools(opened)
        assert "approve" not in await toolset.get_tools(closed)
        assert "`approve`" not in await toolset.get_instructions(opened)
        assert "`approve`: The books are closed." in await toolset.get_instructions(closed)


async def test_the_derived_block_is_memoised_by_which_tools_are_left_out(monkeypatch):
    """A step pays the derivation only for a combination the toolset has not seen."""
    calls: list[frozenset[str]] = []
    derive = spec_toolset._derive_instructions

    def counting(specs: Any, *args: Any, **kwargs: Any) -> str:
        calls.append(frozenset(specs))
        return derive(specs, *args, **kwargs)

    monkeypatch.setattr(spec_toolset, "_derive_instructions", counting)
    toolset = SpecToolset(
        {"list_widgets": list_spec(), "approve": _conditioned_spec(_books_open_for)}
    )

    for username in ("closed", "also-closed", "closed"):
        await toolset.get_instructions(ctx_for(User(username=username)))

    assert calls == [frozenset({"list_widgets"})]


@pytest.mark.django_db
async def test_a_condition_that_queries_runs_off_the_event_loop():
    """Django raises ``SynchronousOnlyOperation`` for ORM access on the loop."""
    toolset = SpecToolset({"approve": _conditioned_spec(lambda: Widget.objects.exists())})

    tools = await toolset.get_tools(ctx_for(User(username="u")))

    assert tools == {}


@pytest.mark.parametrize(
    "arrived",
    [None, RequestFactory().get("/", {"scope": "all"})],
    ids=["no request", "the request the run arrived on"],
)
async def test_a_condition_sees_the_request_and_user_its_call_would(arrived):
    """Asked at listing time, then again by the call's own enforcement."""
    seen: list[tuple[Any, Any]] = []

    def records(request: Any, user: Any) -> bool:
        seen.append((request, user))
        return True

    user = User(username="u")
    deps = AgentDeps(user=user)
    if arrived is not None:
        deps.call_context = {"request": arrived}
    toolset = SpecToolset({"approve": _conditioned_spec(records)})
    ctx = ctx_for(deps=deps)

    tools = await toolset.get_tools(ctx)
    await toolset.call_tool("approve", {}, ctx, tools["approve"])

    listed, called = seen
    assert listed[0] is arrived and called[0] is arrived
    assert listed[1] is user and called[1] is user


async def test_a_call_seeds_override_reaches_a_condition_at_listing_time():
    """The listing asks a condition with the seeds the call would register."""

    class Tenanted(SpecToolset):
        def call_seeds(self, *, ctx, tool_name):
            return {"tenant": lambda: ctx.deps.user.username}

    toolset = Tenanted({"approve": _conditioned_spec(lambda tenant: tenant == "acme")})

    assert "approve" in await toolset.get_tools(ctx_for(User(username="acme")))
    assert "approve" not in await toolset.get_tools(ctx_for(User(username="globex")))


async def test_a_condition_is_asked_on_the_toolsets_own_dispatch_thread():
    """``thread_sensitive`` and ``executor`` govern the listing as they do the call."""
    threads: list[str] = []

    def records_thread() -> bool:
        threads.append(threading.current_thread().name)
        return True

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="conditions") as pool:
        toolset = SpecToolset(
            {"approve": _conditioned_spec(records_thread)},
            thread_sensitive=False,
            executor=pool,
        )
        await toolset.get_tools(ctx_for(User(username="u")))

    assert threads and threads[0].startswith("conditions")


@pytest.mark.django_db
async def test_a_listing_time_condition_closes_the_connection_it_opened() -> None:
    """The dispatch's connection cleanup, owed by the listing's hop as well."""
    _assert_a_close_is_observable()
    asked_on: dict[str, int] = {}

    def queries() -> bool:
        asked_on["thread"] = threading.get_ident()
        return not Widget.objects.exists()

    toolset = SpecToolset({"approve": _conditioned_spec(queries)})
    ctx = ctx_for(User(username="u"))

    await sync_to_async(connections.close_all, thread_sensitive=True)()
    assert (await sync_to_async(_dispatch_thread_state, thread_sensitive=True)())[1] is False

    tools = await toolset.get_tools(ctx)
    ident, open_after_listing = await sync_to_async(_dispatch_thread_state, thread_sensitive=True)()
    await toolset.call_tool("approve", {}, ctx, tools["approve"])
    _, open_after_call = await sync_to_async(_dispatch_thread_state, thread_sensitive=True)()

    assert ident == asked_on["thread"], "the probe did not land on the condition's thread"
    assert open_after_listing is False
    assert open_after_call is False


async def test_an_omitted_tool_is_absent_from_what_the_model_is_offered():
    """Through a real run: the model's tool list lacks it, its instructions name it."""
    offered: list[list[str]] = []
    instructions: list[str | None] = []
    calls_ping = tool_calling_model("ping")

    def recording(messages: Any, info: Any) -> Any:
        offered.append(sorted(tool.name for tool in info.function_tools))
        instructions.append(messages[-1].instructions)
        return calls_ping.function(messages, info)

    toolset = SpecToolset(
        {
            "ping": ServiceSpec(service=_approve, permissions=allow(), atomic=False),
            "approve": _conditioned_spec(lambda: False),
        }
    )
    agent = Agent(FunctionModel(recording), deps_type=AgentDeps, toolsets=[toolset])

    result = await agent.run("go", deps=AgentDeps(user=User(username="u")))

    assert result.output == "done"
    assert offered == [["ping"], ["ping"]]
    assert all("`approve`: The books are closed." in (text or "") for text in instructions)


async def test_a_model_calling_an_omitted_tool_gets_the_unknown_tool_retry():
    """Nothing new on the call path: an omitted name is simply not a tool."""
    ran: list[bool] = []
    retries: list[str] = []

    def approve(**_: Any) -> None:
        """Approve."""
        ran.append(True)

    def calls_approve(messages: Any, info: Any) -> Any:
        retry = [p for p in messages[-1].parts if is_tool_retry(p)]
        if retry:
            retries.append(str(retry[0].content))
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart(tool_name="approve", args={})])

    toolset = SpecToolset(
        {
            "approve": ServiceSpec(
                service=approve,
                atomic=False,
                affordances=[
                    Affordance(
                        code="books_closed", reason="The books are closed.", when=lambda: False
                    )
                ],
                permissions=allow(),
            )
        }
    )
    agent = Agent(FunctionModel(calls_approve), deps_type=AgentDeps, toolsets=[toolset])

    result = await agent.run("go", deps=AgentDeps(user=User(username="u")))

    assert result.output == "done"
    assert ran == []
    assert "Unknown tool name: 'approve'" in retries[0]


# --- registered pool seeds ----------------------------------------------------
#
# ``pool_seeds=`` is a project's own always-available seeds: what hangs off
# ``request`` over HTTP and has no channel off it. A spec reading one has to see
# it wherever the toolset dispatches or asks a condition, or it works when the
# kernel's ``dispatch`` is called directly and fails through the toolset.


def _tenant_seeds() -> PoolSeeds:
    """A ``tenant`` seed derived from the acting user, as a project's would be."""
    return DEFAULT_POOL_SEEDS.extend(tenant=lambda user: f"tenant-of-{user.username}")


@dataclass
class _TenantRow:
    tenant: str


def _tenant_input() -> django_service_specs.DataclassValidator:
    """A Validator taking one ``tenant`` field, named like the registered seed."""
    return django_service_specs.DataclassValidator(_TenantRow)


async def test_a_registered_seed_reaches_a_service_called_through_the_toolset():
    def stamp(tenant: str) -> dict[str, str]:
        """Stamp the caller's tenant."""
        return {"tenant": tenant}

    toolset = SpecToolset(
        {"stamp": ServiceSpec(service=stamp, atomic=False, permissions=allow())},
        pool_seeds=_tenant_seeds(),
    )

    result = await _call(toolset, "stamp", User(username="acme"))

    assert result == {"tenant": "tenant-of-acme"}


async def test_a_registered_seed_outranks_a_model_argument_of_the_same_name():
    """On a selector nothing validates the spread, so reservation is the guard."""

    def tenant_rows(tenant: str) -> list[dict[str, str]]:
        """The caller's tenant, as a one-row collection."""
        return [{"tenant": tenant}]

    toolset = SpecToolset(
        {
            "rows": SelectorSpec(
                kind=SelectorKind.LIST,
                selector=tenant_rows,
                reads=Parameters.of(Parameter("tenant", "string", reflected=True)),
                presenter=RowPresenter(OutputField("tenant", "string")),
                permissions=allow(),
            )
        },
        pool_seeds=_tenant_seeds(),
    )

    result = await _call(toolset, "rows", User(username="acme"), {"tenant": "globex"})

    assert _rows(result) == [{"tenant": "tenant-of-acme"}]
    schema = toolset._tool_defs["rows"].parameters_json_schema
    assert "tenant" not in schema.get("properties", {})
    assert "tenant" not in schema.get("required", [])


def test_a_shape_a_schema_fragment_drops_is_told_nothing():
    """An input fragment wins key by key, so its own ``properties`` can drop a shape."""
    spec = list_spec(
        shapes=Parameters.of(Parameter("fields", "string")),
        metadata={"json_schema": {"input": {"properties": {}}}},
    )

    schema = _input_schema_of(SpecToolset({"rows": spec}), "rows")

    assert list(schema["properties"]) == ["page", "limit"]


async def test_a_validator_field_named_like_a_seed_is_the_model_s_to_send():
    """The field is validated into ``data``; the service's ``tenant`` stays the seed's."""

    def move(data: _TenantRow, tenant: str) -> dict[str, str]:
        """Move a row to the tenant the caller names."""
        return {"destination": data.tenant, "tenant": tenant}

    toolset = SpecToolset(
        {
            "move": ServiceSpec(
                service=move,
                validator=_tenant_input(),
                atomic=False,
                permissions=allow(),
            )
        },
        pool_seeds=_tenant_seeds(),
    )

    result = await _call(toolset, "move", User(username="acme"), {"tenant": "globex"})

    assert _input_schema_of(toolset, "move")["required"] == ["tenant"]
    assert result == {"destination": "globex", "tenant": "tenant-of-acme"}


async def test_a_row_field_named_like_a_seed_is_validated_on_a_many_spec():
    """Each row's field is validated into ``data``; the service's ``tenant`` stays the seed's."""

    def move_all(data: list[_TenantRow], tenant: str) -> list[dict[str, str]]:
        """Move each row to the tenant it names."""
        return [{"destination": row.tenant, "tenant": tenant} for row in data]

    toolset = SpecToolset(
        {
            "move_all": ServiceSpec(
                service=move_all,
                validator=_tenant_input(),
                many=True,
                atomic=False,
                permissions=allow(),
            )
        },
        pool_seeds=_tenant_seeds(),
    )

    result = await _call(
        toolset,
        "move_all",
        User(username="acme"),
        {"items": [{"tenant": "globex"}, {"tenant": "initech"}]},
    )

    assert result == {
        "items": [
            {"destination": "globex", "tenant": "tenant-of-acme"},
            {"destination": "initech", "tenant": "tenant-of-acme"},
        ]
    }


def _books_seeds() -> PoolSeeds:
    """Books are open for the user named ``open`` and nobody else."""
    return DEFAULT_POOL_SEEDS.extend(books_open=lambda user: user.username == "open")


@pytest.mark.parametrize(("username", "listed"), [("open", True), ("closed", False)])
async def test_a_condition_reading_a_registered_seed_is_asked_with_it(username, listed):
    """The listing and the call ask the condition against the same seeds."""
    toolset = SpecToolset(
        {"approve": _conditioned_spec(lambda books_open: books_open)},
        pool_seeds=_books_seeds(),
    )
    ctx = ctx_for(User(username=username))

    tools = await toolset.get_tools(ctx)
    instructions = await toolset.get_instructions(ctx)

    assert ("approve" in tools) is listed
    assert ("`approve`: The books are closed." in instructions) is not listed
    if listed:
        assert await toolset.call_tool("approve", {}, ctx, tools["approve"]) == {"approved": True}


@pytest.mark.parametrize(
    "books_open",
    [
        pytest.param(lambda call_context: call_context["action"] == "approve", id="call_context"),
        pytest.param(lambda request: request is None, id="request"),
    ],
)
async def test_a_condition_reading_a_seed_that_reads_the_call_is_listed_and_answers(books_open):
    """A seed resolving from the call's context resolves in the listing's pool too."""
    toolset = SpecToolset(
        {"approve": _conditioned_spec(lambda books_open: books_open)},
        pool_seeds=DEFAULT_POOL_SEEDS.extend(books_open=books_open),
    )
    ctx = ctx_for(User(username="u"))

    tools = await toolset.get_tools(ctx)

    assert "approve" in tools
    assert await toolset.call_tool("approve", {}, ctx, tools["approve"]) == {"approved": True}


async def test_a_seed_named_argument_a_spec_never_declares_is_refused():
    """A registered seed's name is not an argument of a spec that never reads it."""

    def ping() -> dict[str, bool]:
        """Ping."""
        return {"ok": True}

    def toolset(**kwargs: Any) -> SpecToolset:
        return SpecToolset(
            {"ping": ServiceSpec(service=ping, atomic=False, permissions=allow())},
            **kwargs,
        )

    user = User(username="acme")
    for options in ({}, {"pool_seeds": _tenant_seeds()}):
        with pytest.raises(ModelRetry, match=r"^tenant: Unknown argument\.$"):
            await _call(toolset(**options), "ping", user, {"tenant": "globex"})
        ignoring = toolset(**options, unknown_arguments=UnknownArguments.IGNORE)
        assert await _call(ignoring, "ping", user, {"tenant": "globex"}) == {"ok": True}


@pytest.mark.parametrize(
    ("policy", "served"),
    [(UnknownArguments.REJECT, False), (UnknownArguments.IGNORE, True)],
    ids=["reject", "ignore"],
)
async def test_a_dispatcher_name_the_model_sends_is_refused_or_dropped(policy, served):
    """``user`` is the dispatcher's, so a selector reading it is never handed the model's."""

    def mine(user: Any) -> dict[str, str]:
        """Who is asking."""
        return {"who": str(user)}

    toolset = SpecToolset(
        {"mine": SelectorSpec(selector=mine, kind=SelectorKind.RETRIEVE, permissions=allow())},
        unknown_arguments=policy,
    )
    user = User(username="acme")

    assert "user" not in _input_schema_of(toolset, "mine").get("properties", {})
    if served:
        assert await _call(toolset, "mine", user, {"user": "globex"}) == {"who": "acme"}
    else:
        with pytest.raises(ModelRetry, match=r"^user: Unknown argument\.$"):
            await _call(toolset, "mine", user, {"user": "globex"})


@dataclass
class _NoteInput:
    text: str


class _UnsendableValidator(django_service_specs.DataclassValidator):
    """A Validator answering that it requires ``user``, which no call can send."""

    unsendable = frozenset({"user"})


class _PartialUnsendableValidator(_UnsendableValidator):
    """The same answer, from a Validator that can validate a partial call."""

    supports_partial = True


def _keep_note(data: Any) -> Any:
    """Keep a note."""
    return asdict(data) if is_dataclass(data) else dict(data)


def _note_spec(
    validator: django_service_specs.Validator, *, partial: bool = False
) -> django_service_specs.ServiceSpec:
    return django_service_specs.ServiceSpec(
        service=_keep_note,
        validator=validator,
        partial=partial,
        atomic=False,
        permissions=[django_service_specs.Unrestricted()],
    )


@pytest.mark.parametrize("policy", [UnknownArguments.REJECT, UnknownArguments.IGNORE])
def test_a_field_the_validator_calls_unsendable_is_refused_naming_the_tool(policy):
    """A Validator's ``unsendable`` fields are refused as unknown arguments under ``REJECT``."""
    with pytest.raises(
        ImproperlyConfigured,
        match=(
            r"^SpecToolset tool 'note': the input requires \['user'\], "
            r"which no call can send"
        ),
    ):
        SpecToolset(
            {"note": _note_spec(_UnsendableValidator(_NoteInput))}, unknown_arguments=policy
        )


async def test_a_validator_that_does_not_answer_unsendable_builds_and_serves():
    """A Validator without the attribute names no field, and its absence means none."""
    toolset = SpecToolset({"note": _note_spec(django_service_specs.DataclassValidator(_NoteInput))})

    assert await _call(toolset, "note", User(username="u"), {"text": "hi"}) == {"text": "hi"}


async def test_an_empty_answer_builds_and_serves():
    """An empty ``unsendable`` refuses nothing."""

    class Sendable(django_service_specs.DataclassValidator):
        unsendable: frozenset[str] = frozenset()

    toolset = SpecToolset({"note": _note_spec(Sendable(_NoteInput))})

    assert await _call(toolset, "note", User(username="u"), {"text": "hi"}) == {"text": "hi"}


def test_a_partial_service_is_not_refused_for_an_unsendable_field():
    """A partial call may leave any field out, so an unsendable field still builds."""
    toolset = SpecToolset(
        {"note": _note_spec(_PartialUnsendableValidator(_NoteInput), partial=True)}
    )

    assert "note" in toolset._tool_defs


@pytest.mark.parametrize("declare", [shaping, reading], ids=["shape", "read"])
def test_a_channel_named_after_a_registered_seed_is_refused(declare):
    """A registered seed is reserved like a built-in one, so a channel cannot name it."""
    with pytest.raises(ImproperlyConfigured, match="tenant"):
        SpecToolset(
            declare({"list": list_spec()}, {"list": [Parameter("tenant", "string")]}),
            pool_seeds=_tenant_seeds(),
        )


@pytest.mark.parametrize("seat", ["reads", "shapes"])
def test_a_mapping_read_named_after_a_registered_seed_is_refused_at_build(seat):
    """A mapping-built tool is refused the same collisions as a registry-built one."""
    spec = replace(list_spec(), **{seat: Parameters.of(Parameter("tenant", "string"))})

    with pytest.raises(ImproperlyConfigured, match=r"^SpecToolset tool 'list': .*'tenant'"):
        SpecToolset({"list": spec}, pool_seeds=_tenant_seeds())


def test_a_lookup_read_named_after_a_registered_seed_is_refused_at_build():
    """A read the target lookup takes is declared on the lookup, and checked there too."""

    def get_by_tenant(*, tenant: str):
        """The row of one tenant."""
        return Widget.objects.get(name=tenant)

    spec = ServiceSpec(
        service=lambda *, instance: {"name": instance.name},
        instance_selector_spec=SelectorSpec(
            kind=SelectorKind.RETRIEVE,
            selector=get_by_tenant,
            reads=Parameters.of(Parameter("tenant", "string", required=True)),
        ),
        permissions=allow(),
        atomic=False,
    )

    with pytest.raises(ImproperlyConfigured, match=r"^SpecToolset tool 'rename': .*'tenant'"):
        SpecToolset({"rename": spec}, pool_seeds=_tenant_seeds())


def _seeds_named(name: str) -> PoolSeeds:
    return DEFAULT_POOL_SEEDS.extend(**{name: lambda user: []})


@pytest.mark.parametrize("seed", ["items", "request", "view"])
def test_a_many_list_named_after_a_seed_is_refused_at_build(seed):
    """A seed of the list's name makes the kernel refuse every call sending it."""
    spec = bulk_spec(many_argument=seed)

    with pytest.raises(ImproperlyConfigured, match=rf"^SpecToolset tool 'bulk': .*'{seed}'"):
        SpecToolset({"bulk": spec}, pool_seeds=_seeds_named("items"))


def test_a_many_list_beside_a_seed_of_another_name_builds():
    toolset = SpecToolset({"bulk": bulk_spec()}, pool_seeds=_seeds_named("tenant"))

    assert set(toolset.specs) == {"bulk"}


def test_a_seed_named_like_the_default_list_does_not_touch_a_spec_with_no_list():
    """Only a ``many`` service has a list to collide; a selector's ``items`` default is unused."""
    toolset = SpecToolset(
        {"create": create_spec(), "list": list_spec()}, pool_seeds=_seeds_named("items")
    )

    assert set(toolset.specs) == {"create", "list"}


# --- what a call needs from the model -----------------------------------------
#
# A tool's input schema is the model's only account of what to send. A spec
# declares the reads its callables take, and the toolset takes out the names the
# server fills: those are not advertised, every other read without a default is
# required, and a call missing one is handed back as a retry instead of reaching
# the callable as a ``TypeError``.


class _NameSerializer(RowPresenter):
    """A row as its ``name``: the presenter every name-only spec here renders with."""

    def __init__(self) -> None:
        super().__init__(OutputField("name", "string"))


def _input_schema_of(toolset: SpecToolset, name: str) -> dict[str, Any]:
    return toolset._tool_defs[name].parameters_json_schema


def _name_list_spec(selector: Any, **kwargs: Any) -> SelectorSpec:
    kwargs.setdefault("permissions", allow())
    return SelectorSpec(
        kind=SelectorKind.LIST, selector=selector, presenter=_NameSerializer(), **kwargs
    )


def _update_spec(**kwargs: Any) -> ServiceSpec:
    kwargs.setdefault(
        "instance_selector_spec",
        by_pk(get_any_widget),
    )
    kwargs.setdefault("validator", widget_input())
    return ServiceSpec(
        service=update_widget,
        permissions=allow(),
        atomic=False,
        **kwargs,
    )


def test_a_selector_parameter_without_a_default_is_required():
    """``get_widget(user, pk)`` cannot run without ``pk``, which only the model sends."""
    schema = _input_schema_of(SpecToolset({"get": retrieve_spec()}), "get")

    assert schema["properties"] == {"pk": {"type": "integer"}}
    assert schema["required"] == ["pk"]


def recent_names(user, progress, newest_first: bool = True):
    """The acting user's names, newest first unless told otherwise."""
    names = ["older", "newer"]
    return [{"name": name} for name in (reversed(names) if newest_first else names)]


async def test_a_dispatcher_seed_is_neither_advertised_nor_required():
    """``progress`` is the dispatcher's own: always in the pool, never the model's."""
    toolset = SpecToolset(
        {
            "recent": _name_list_spec(
                recent_names,
                reads=Parameters.of(Parameter("newest_first", "boolean", default=True)),
            )
        }
    )

    schema = _input_schema_of(toolset, "recent")
    result = await _call(toolset, "recent", User(username="u"))

    assert "progress" not in schema["properties"]
    assert "progress" not in schema.get("required", [])
    assert schema["properties"]["newest_first"] == {"type": "boolean", "default": True}
    assert "required" not in schema
    assert _rows(result) == [{"name": "newer"}, {"name": "older"}]


def names_for_tenant(tenant):
    """The names in the caller's tenant."""
    return [{"name": tenant}]


async def test_a_seed_parameter_the_model_leaves_out_is_not_refused():
    """A registered seed has no default, and the model is never asked for it."""
    toolset = SpecToolset({"names": _name_list_spec(names_for_tenant)}, pool_seeds=_tenant_seeds())

    result = await _call(toolset, "names", User(username="acme"))

    assert _rows(result) == [{"name": "tenant-of-acme"}]


def names_in_project(user, project_pk):
    """The names in one project."""
    return [{"name": f"project-{project_pk}"}]


@pytest.mark.parametrize("source", ["registry", "mapping"])
async def test_a_read_s_default_answers_for_the_parameter_it_fills(source):
    """A read with a default fills the parameter when the model leaves it out."""
    reads = {"in_project": [Parameter("project_pk", "integer", default=7)]}
    specs = {"in_project": _name_list_spec(names_in_project)}
    toolset = SpecToolset(
        reading(specs, reads)
        if source == "registry"
        else {
            name: replace(spec, reads=Parameters.of(*reads[name])) for name, spec in specs.items()
        }
    )

    schema = _input_schema_of(toolset, "in_project")
    result = await _call(toolset, "in_project", User(username="u"))

    assert "project_pk" not in schema.get("required", [])
    assert schema["properties"]["project_pk"]["type"] == "integer"
    assert _rows(result) == [{"name": "project-7"}]


@pytest.mark.parametrize("source", ["registry", "mapping"])
async def test_a_read_s_default_never_replaces_the_value_sent_from_either_source(source):
    """The call applies a read's default only where the model sent nothing."""
    reads = {"in_project": [Parameter("project_pk", "integer", default=7)]}
    specs = {"in_project": _name_list_spec(names_in_project)}
    toolset = SpecToolset(
        reading(specs, reads)
        if source == "registry"
        else {
            name: replace(spec, reads=Parameters.of(*reads[name])) for name, spec in specs.items()
        }
    )

    result = await _call(toolset, "in_project", User(username="u"), {"project_pk": 3})

    assert _rows(result) == [{"name": "project-3"}]


@pytest.mark.parametrize("source", ["registry", "mapping"])
async def test_a_null_under_a_read_is_the_model_declining_it(source):
    """A ``null`` under a read is the model declining it, so the default answers."""
    reads = {"in_project": [Parameter("project_pk", "integer", default=7)]}
    specs = {"in_project": _name_list_spec(names_in_project)}
    toolset = SpecToolset(
        reading(specs, reads)
        if source == "registry"
        else {
            name: replace(spec, reads=Parameters.of(*reads[name])) for name, spec in specs.items()
        }
    )

    result = await _call(toolset, "in_project", User(username="u"), {"project_pk": None})

    assert _rows(result) == [{"name": "project-7"}]


@pytest.mark.parametrize("source", ["registry", "mapping"])
async def test_a_null_under_a_required_read_is_retried_alike_from_either_source(source):
    """A required read sent as ``null`` is refused by the kernel, in its own words."""
    reads = {"in_project": [Parameter("project_pk", "integer", required=True)]}
    specs = {"in_project": _name_list_spec(names_in_project)}
    toolset = SpecToolset(
        reading(specs, reads)
        if source == "registry"
        else {
            name: replace(spec, reads=Parameters.of(*reads[name])) for name, spec in specs.items()
        }
    )

    with pytest.raises(ModelRetry, match=r"^project_pk: This field is required\.$"):
        await _call(toolset, "in_project", User(username="u"), {"project_pk": None})


async def test_a_required_read_with_no_default_is_handed_back_when_left_out():
    """With no default, the value reaches the selector only when the model sends it."""
    toolset = SpecToolset(
        reading(
            {"in_project": _name_list_spec(names_in_project)},
            {"in_project": [Parameter("project_pk", "integer", required=True)]},
        )
    )

    schema = _input_schema_of(toolset, "in_project")
    with pytest.raises(ModelRetry) as raised:
        await _call(toolset, "in_project", User(username="u"))
    result = await _call(toolset, "in_project", User(username="u"), {"project_pk": 3})

    assert schema["required"] == ["project_pk"]
    assert schema["properties"]["project_pk"]["type"] == "integer"
    assert str(raised.value) == "Missing required argument(s): `project_pk`."
    assert _rows(result) == [{"name": "project-3"}]


class _ProjectScopedToolset(SpecToolset):
    """Fills ``project_pk`` with a seed from an override, as a project scoping
    every call to the run's project might."""

    def call_seeds(self, *, ctx, tool_name):
        return {"project_pk": lambda: 42}


async def test_a_name_only_a_call_seeds_override_fills_is_asked_of_the_model():
    """An override is code the schema cannot read, so its name is advertised as required."""
    toolset = _ProjectScopedToolset({"in_project": _in_project_reflecting()})

    schema = _input_schema_of(toolset, "in_project")
    left_out = await _call(toolset, "in_project", User(username="u"))
    sent = await _call(toolset, "in_project", User(username="u"), {"project_pk": 5})

    assert schema["required"] == ["project_pk"]
    assert _rows(left_out) == [{"name": "project-42"}]
    assert _rows(sent) == [{"name": "project-42"}]


def _in_project_reflecting() -> SelectorSpec:
    """``names_in_project`` reflects its ``project_pk`` as a parameter with no default."""
    return _name_list_spec(
        names_in_project,
        reads=Parameters.of(Parameter("project_pk", "integer", required=True, reflected=True)),
    )


def names_in_hidden_project(user, project_pk: int):
    """``names_in_project``, whose spec declares no read of ``project_pk``."""
    return [{"name": f"project-{project_pk}"}]


async def test_a_name_a_call_seeds_override_fills_is_kept_from_the_model():
    """An override is told first to leave the name out of the spec's reads."""
    toolset = _ProjectScopedToolset({"in_project": _name_list_spec(names_in_hidden_project)})

    schema = _input_schema_of(toolset, "in_project")
    result = await _call(toolset, "in_project", User(username="u"))
    with pytest.raises(ModelRetry) as raised:
        await _call(toolset, "in_project", User(username="u"), {"project_pk": 5})

    assert "project_pk" not in schema.get("properties", {})
    assert "project_pk" not in schema.get("required", [])
    assert _rows(result) == [{"name": "project-42"}]
    assert str(raised.value) == "project_pk: Unknown argument."


def names_in_hidden_project_or_filtered(user, project_pk: int, **filters: Any):
    """``names_in_hidden_project``, open to any filter a caller names."""
    return names_in_hidden_project(user, project_pk)


async def test_a_value_the_model_sends_for_an_undeclared_name_never_reaches_the_selector():
    """Under ``IGNORE`` the model's value for an undeclared name is dropped."""
    toolset = _ProjectScopedToolset(
        {"in_project": _name_list_spec(names_in_hidden_project)},
        unknown_arguments=UnknownArguments.IGNORE,
    )

    result = await _call(toolset, "in_project", User(username="u"), {"project_pk": 5})

    assert _rows(result) == [{"name": "project-42"}]


async def test_a_marked_name_sent_to_an_open_selector_is_refused():
    """A bare ``**filters`` does not open the argument set under ``REJECT``."""
    toolset = _ProjectScopedToolset(
        {"in_project": _name_list_spec(names_in_hidden_project_or_filtered)}
    )

    with pytest.raises(ModelRetry) as raised:
        await _call(toolset, "in_project", User(username="u"), {"project_pk": 5})

    assert str(raised.value) == "project_pk: Unknown argument."


async def test_a_name_a_call_seeds_override_fills_is_declared_to_the_toolset():
    """A seed registered by the mount keeps a name out of the schema, where an override cannot."""
    toolset = SpecToolset(
        {"in_project": _in_project_reflecting()},
        pool_seeds=DEFAULT_POOL_SEEDS.extend(project_pk=lambda: 7),
    )

    schema = _input_schema_of(toolset, "in_project")
    left_out = await _call(toolset, "in_project", User(username="u"))
    sent = await _call(toolset, "in_project", User(username="u"), {"project_pk": 5})

    assert "project_pk" not in schema.get("properties", {})
    assert _rows(left_out) == [{"name": "project-7"}]
    assert _rows(sent) == [{"name": "project-7"}]


async def test_a_seed_an_override_returns_for_a_declared_read_is_refused():
    """A seed over a declared read would serve the wrong row silently: project 42 for project 5."""
    toolset = _ProjectScopedToolset(
        reading(
            {"in_project": _name_list_spec(names_in_project)},
            {"in_project": [Parameter("project_pk", "integer", default=0)]},
        )
    )

    with pytest.raises(
        ImproperlyConfigured, match=r"\['project_pk'\], which a registered pool seed"
    ):
        await _call(toolset, "in_project", User(username="u"), {"project_pk": 5})


def names_under(user, ceiling):
    """The names priced at or below the ceiling."""
    return [{"name": name} for name, price in (("cheap", 5), ("dear", 15)) if price <= ceiling]


#: ``ceiling`` as a library compiling ``names_under`` reflects it off the
#: signature: required, since the parameter has no default.
_CEILING = Parameter("ceiling", "integer", required=True, reflected=True)


#: ``ceiling`` as an author declares it beside a provider that may fill it:
#: optional, so a call the provider serves is not refused for leaving it out.
_OPEN_CEILING = Parameter("ceiling", "integer")


def _under(
    selector: Any = names_under,
    *reads: Parameter,
    ceiling: Parameter = _CEILING,
    **kwargs: Any,
) -> SelectorSpec:
    """A name list over ``selector``, reading ``ceiling`` and ``reads``."""
    return _name_list_spec(selector, reads=Parameters.of(ceiling, *reads), **kwargs)


class _Ceiling(TypedDict):
    ceiling: int


def _typed_ceiling(project_pk=None) -> _Ceiling:
    """``_ceiling_from_project``, with the keys it returns declared."""
    return _Ceiling(ceiling=_ceiling_from_project(project_pk)["ceiling"])


async def test_a_name_a_typed_provider_returns_is_not_asked_for():
    """A ``TypedDict`` return annotation tells the kernel which parameters a provider fills."""
    toolset = SpecToolset(
        reading(
            {"under": _under(kwargs=_typed_ceiling)},
            {"under": [Parameter("project_pk", "string")]},
        )
    )

    schema = _input_schema_of(toolset, "under")
    result = await _call(toolset, "under", User(username="u"), {"project_pk": "10"})

    assert "ceiling" not in schema["properties"]
    assert "ceiling" not in schema.get("required", [])
    assert _rows(result) == [{"name": "cheap"}]


async def test_an_optional_read_beside_an_untyped_provider_stays_the_model_s_to_send():
    """A provider returning an undeclared ``dict`` may fill any parameter, so no read is dropped."""
    toolset = SpecToolset(
        reading(
            {"under": _under(kwargs=_ceiling_from_project, ceiling=_OPEN_CEILING)},
            {"under": [Parameter("project_pk", "string")]},
        )
    )

    schema = _input_schema_of(toolset, "under")
    result = await _call(toolset, "under", User(username="u"), {"project_pk": "10"})

    assert schema["properties"]["ceiling"] == {"type": "integer"}
    assert "required" not in schema
    assert _rows(result) == [{"name": "cheap"}]


def names_in_band(user, ceiling, floor: int):
    """The names priced inside the band."""
    return [{"name": "cheap"}] if floor <= 5 <= ceiling else []


#: ``floor`` declared required, a declaration rather than an inference from a
#: missing default, as a library compiles a parameter it is told is required.
_FLOOR = Parameter("floor", "integer", required=True)


def test_a_marked_parameter_beside_an_untyped_provider_is_advertised_and_not_required():
    """A required read beside an unreadable provider is advertised, not required of the model."""
    toolset = SpecToolset(
        {
            "band": _under(
                names_in_band, _FLOOR, kwargs=_ceiling_from_project, ceiling=_OPEN_CEILING
            )
        },
    )

    schema = _input_schema_of(toolset, "band")

    assert schema["properties"]["floor"] == {"type": "integer"}
    assert "required" not in schema


def _untyped_band(view):
    """Both ends of the band, with nothing declared about the keys."""
    return {"ceiling": 10, "floor": 2}


async def test_an_untyped_provider_filling_a_marked_parameter_is_not_refused():
    """A call leaving out ``floor`` is served, since the provider may fill it."""
    toolset = SpecToolset(
        {"band": _under(names_in_band, _FLOOR, kwargs=_untyped_band, ceiling=_OPEN_CEILING)}
    )

    result = await _call(toolset, "band", User(username="u"))

    assert _rows(result) == [{"name": "cheap"}]


async def test_an_untyped_provider_leaving_a_marked_parameter_unfilled_is_handed_back():
    """Not requiring ``floor`` is safe: the kernel refuses the call at the assembled pool."""
    toolset = SpecToolset(
        {"band": _under(names_in_band, _FLOOR, kwargs=_ceiling_from_project, ceiling=_OPEN_CEILING)}
    )

    with pytest.raises(ModelRetry) as raised:
        await _call(toolset, "band", User(username="u"))

    assert str(raised.value) == "floor: This field is required."


def test_a_required_read_its_default_fills_is_not_required_beside_an_untyped_provider():
    """The declaration requires ``floor``, and its default fills it on every call."""
    floor = Parameter("floor", "integer", required=True, default=2)
    toolset = SpecToolset(
        {"band": _under(names_in_band, floor, kwargs=_untyped_band, ceiling=_OPEN_CEILING)}
    )

    assert "floor" not in _input_schema_of(toolset, "band").get("required", [])


def _unresolvable_ceiling(project_pk=None) -> _NotDeclaredAnywhere:  # noqa: F821
    """A provider whose return annotation names something that does not exist."""
    return _ceiling_from_project(project_pk)


def test_a_provider_whose_annotation_cannot_be_resolved_counts_as_untyped():
    """An unreadable annotation says nothing about the keys, so nothing is dropped as filled."""
    toolset = SpecToolset(
        {"under": _under(kwargs=_unresolvable_ceiling, ceiling=_OPEN_CEILING)},
    )

    schema = _input_schema_of(toolset, "under")

    assert schema["properties"]["ceiling"] == {"type": "integer"}
    assert "required" not in schema


_Owner = TypeVar("_Owner")


class _ScopeExtras(TypedDict, total=False):
    currency: str


class _Scope(_ScopeExtras):
    """``ceiling`` always, ``currency`` when the provider has one."""

    ceiling: int


class _ScopeFor(TypedDict, Generic[_Owner]):
    ceiling: int
    owner: _Owner


def _scope(view) -> _Scope:
    return _Scope(ceiling=10)


def _scope_for(view) -> _ScopeFor[int]:
    return _ScopeFor(ceiling=10, owner=1)


def _scope_as_a_dict(view) -> dict[str, int]:
    return {"ceiling": 10}


class _ScopeOrDecline(TypedDict):
    """``currency`` always, ``ceiling`` or ``UNSET`` to leave it to the caller."""

    ceiling: int | UnsetType
    currency: str | None


def _scope_or_decline(view) -> _ScopeOrDecline:
    return _ScopeOrDecline(ceiling=UNSET, currency="EUR")


class _ScopeWithAnUnresolvableKey(TypedDict):
    ceiling: _NotDeclaredAnywhere  # noqa: F821


def _scope_with_an_unresolvable_key(view) -> _ScopeWithAnUnresolvableKey:
    return {"ceiling": 10}


def _scope_for_an_unresolvable_view(view: _NotDeclaredAnywhere) -> _Scope:  # noqa: F821
    """A readable return annotation beside a ``TYPE_CHECKING``-only parameter annotation."""
    return _Scope(ceiling=10)


def name_in_scope(user, ceiling, currency, owner, region):
    """The name inside one scope; every parameter is someone's to fill."""
    return {"name": region}


#: ``name_in_scope``'s parameters as a library compiling it reflects them off
#: the signature: each required, since none has a default.
_SCOPE_READS = Parameters.of(
    *(
        Parameter(name, "string", required=True, reflected=True)
        for name in ("ceiling", "currency", "owner", "region")
    )
)

#: The same reads as that library leaves them beside a provider whose keys it
#: cannot read, which may fill any of them: none required for lacking a default.
_OPEN_SCOPE_READS = Parameters.of(
    *(Parameter(name, "string", reflected=True) for name in _SCOPE_READS.names())
)


def ceiling_name(user, ceiling):
    """The name under one ceiling, which the provider may leave to the caller."""
    return {"name": str(ceiling)}


def test_a_schema_whose_every_read_may_be_declined_requires_nothing():
    """With nothing left required, the kernel writes no ``required`` key at all."""
    spec = SelectorSpec(
        kind=SelectorKind.RETRIEVE,
        selector=ceiling_name,
        reads=Parameters.of(_CEILING),
        kwargs=_scope_or_decline,
        presenter=_NameSerializer(),
        permissions=allow(),
    )

    schema = _input_schema_of(SpecToolset({"name": spec}), "name")

    assert list(schema["properties"]) == ["ceiling"]
    assert "required" not in schema


@pytest.mark.parametrize(
    ("provider", "asked", "required"),
    [
        (
            None,
            ["ceiling", "currency", "owner", "region"],
            ["ceiling", "currency", "owner", "region"],
        ),
        (_scope, ["owner", "region"], ["owner", "region"]),
        (_scope_for, ["currency", "region"], ["currency", "region"]),
        (_scope_as_a_dict, ["ceiling", "currency", "owner", "region"], []),
        (lambda view: {"ceiling": 10}, ["ceiling", "currency", "owner", "region"], []),
        (_unresolvable_ceiling, ["ceiling", "currency", "owner", "region"], []),
        (_scope_or_decline, ["ceiling", "owner", "region"], ["owner", "region"]),
        (
            _scope_with_an_unresolvable_key,
            ["ceiling", "currency", "owner", "region"],
            ["currency", "owner", "region"],
        ),
        (_scope_for_an_unresolvable_view, ["owner", "region"], ["owner", "region"]),
    ],
    ids=[
        "no-provider",
        "typed-dict",
        "parameterised",
        "plain-dict",
        "unannotated",
        "unresolvable",
        "declinable-key",
        "unresolvable-key",
        "unresolvable-parameter",
    ],
)
def test_a_providers_return_annotation_decides_what_the_model_is_asked_for(
    provider, asked, required
):
    """A spec with no provider fills nothing, unlike a provider whose keys are undeclared."""
    spec = SelectorSpec(
        kind=SelectorKind.RETRIEVE,
        selector=name_in_scope,
        reads=_SCOPE_READS if provider_keys(provider) is not None else _OPEN_SCOPE_READS,
        presenter=_NameSerializer(),
        permissions=allow(),
        kwargs=provider,
    )
    toolset = SpecToolset({"scoped": spec})

    schema = _input_schema_of(toolset, "scoped")

    assert sorted(schema["properties"]) == asked
    assert schema.get("required", []) == required


async def _advertised(toolset: SpecToolset, name: str) -> tuple[list[str], list[str]]:
    """The properties and the required names of the tool definition the model sees."""
    schema = (await toolset.get_tools(None))[name].tool_def.parameters_json_schema
    return sorted(schema.get("properties", {})), schema.get("required", [])


def get_invoice(*, pk: int, tenant: str) -> dict:
    """Read one invoice inside a tenant."""
    return {"name": f"{tenant}/{pk}"}


def _reflected(selector: Any) -> Parameters:
    """Keyword parameters reflect as required, since none has a default."""
    names = [name for name in inspect.signature(selector).parameters if name != "user"]
    return Parameters.of(
        *(Parameter(name, "string", required=True, reflected=True) for name in names)
    )


def _invoice_spec(selector: Any, provider: Any) -> SelectorSpec:
    return SelectorSpec(
        kind=SelectorKind.RETRIEVE,
        selector=selector,
        reads=_reflected(selector),
        presenter=_NameSerializer(),
        permissions=allow(),
        kwargs=provider,
    )


class _RegionScope(TypedDict):
    """Every key always filled; only an item inside a container may be ``UNSET``."""

    tenant: str
    regions: list[str | UnsetType]
    labels: dict[str, str | UnsetType]


def _region_scope() -> _RegionScope:
    return {"tenant": "acme", "regions": ["eu"], "labels": {}}


def list_invoices(*, pk: int, tenant: str, regions: list, labels: dict) -> dict:
    """Read one invoice inside a tenant's regions."""
    return {"name": f"{tenant}/{pk}"}


async def test_a_key_holding_unset_inside_a_container_is_not_offered_to_the_model():
    """A list or dict key can never be ``UNSET``, so the provider always fills it."""
    toolset = SpecToolset({"read": _invoice_spec(list_invoices, _region_scope)})

    assert await _advertised(toolset, "read") == (["pk"], ["pk"])


class _InvoiceScope(TypedDict):
    tenant: str


class _GroupScope(TypedDict):
    tenant: Group


def _scope_beside_a_hidden_parameter(*, request: HttpRequest) -> _InvoiceScope:
    """A parameter typed with a name imported only under ``TYPE_CHECKING``."""
    return {"tenant": "acme"}


def _scope_with_a_hidden_value(*, request: Any) -> _GroupScope:
    """A ``TypedDict`` value typed with a name imported only under ``TYPE_CHECKING``."""
    return {"tenant": "acme"}


@pytest.mark.parametrize(
    ("provider", "advertised"),
    [
        (_scope_beside_a_hidden_parameter, (["pk"], ["pk"])),
        (_scope_with_a_hidden_value, (["pk", "tenant"], ["pk"])),
    ],
    ids=["parameter", "typed-dict-value"],
)
async def test_an_annotation_imported_only_for_type_checking_leaves_the_keys_readable(
    provider, advertised
):
    """Neither annotation hides which keys the provider returns, so ``pk`` stays required."""
    toolset = SpecToolset({"read": _invoice_spec(get_invoice, provider)})

    assert await _advertised(toolset, "read") == advertised


_Tenant = TypeVar("_Tenant")


class _TenantMayDecline(TypedDict):
    tenant: str | UnsetType


class _GenericTenant(TypedDict, Generic[_Tenant]):
    tenant: _Tenant


def _declining_written_out() -> _TenantMayDecline:
    return {"tenant": UNSET}


def _declining_through_a_parameter() -> _GenericTenant[str | UnsetType]:
    return {"tenant": UNSET}


@pytest.mark.parametrize(
    "provider",
    [_declining_written_out, _declining_through_a_parameter],
    ids=["written-out", "generic"],
)
async def test_a_generic_typed_dict_declines_what_its_binding_declines(provider):
    """``_GenericTenant[str | UnsetType]`` says the same as the written-out annotation."""
    toolset = SpecToolset({"read": _invoice_spec(get_invoice, provider)})

    assert await _advertised(toolset, "read") == (["pk", "tenant"], ["pk"])


class _CeilingOrDecline(TypedDict):
    ceiling: int | UnsetType


def _ceiling_inside_a_project(project_pk=None) -> _CeilingOrDecline:
    """The project's ceiling, or ``UNSET`` outside one to leave it to the caller."""
    return _CeilingOrDecline(ceiling=UNSET if project_pk is None else int(project_pk))


@pytest.mark.parametrize(
    ("args", "rows"),
    [
        ({"project_pk": "10"}, [{"name": "cheap"}]),
        ({"ceiling": 20}, [{"name": "cheap"}, {"name": "dear"}]),
    ],
    ids=["filled", "declined"],
)
async def test_a_key_the_provider_may_decline_stays_the_models_to_send(args, rows):
    """A key the kernel drops when returned as ``UNSET`` is not one the toolset fills."""
    toolset = SpecToolset(
        reading(
            {"under": _under(kwargs=_ceiling_inside_a_project)},
            {"under": [Parameter("project_pk", "string")]},
        )
    )

    schema = _input_schema_of(toolset, "under")

    assert schema["properties"].get("ceiling") == {"type": "integer"}
    assert "ceiling" not in schema.get("required", [])
    assert _rows(await _call(toolset, "under", User(username="u"), args)) == rows


def _nothing_in_scope(view):
    """An untyped provider that has nothing to fill on this call."""
    return {}


def names_under_a_default_ceiling(user, ceiling=10):
    """``names_under``, with a ceiling for when nobody supplies one."""
    return names_under(user, ceiling)


@pytest.mark.parametrize(
    "provider", [_ceiling_inside_a_project, _nothing_in_scope], ids=["declined", "untyped"]
)
async def test_a_parameter_neither_the_provider_nor_the_model_fills_is_asked_for(provider):
    """What the docs promise when a provider declines a parameter, and the model must send it."""
    ceiling = _CEILING if provider is _ceiling_inside_a_project else _OPEN_CEILING
    project_pk = Parameter("project_pk", "string")
    toolset = SpecToolset(
        {
            "under": _under(names_under, project_pk, ceiling=ceiling, kwargs=provider),
            "under_a_default": _under(
                names_under_a_default_ceiling, project_pk, ceiling=_OPEN_CEILING, kwargs=provider
            ),
        }
    )

    with pytest.raises(ModelRetry, match=r"^ceiling: This field is required\.$") as raised:
        await _call(toolset, "under", User(username="u"))
    assert "Missing required argument" not in str(raised.value)
    resent = await _call(toolset, "under", User(username="u"), {"ceiling": 10})
    result = await _call(toolset, "under_a_default", User(username="u"))

    assert _rows(resent) == _rows(result)
    assert _rows(result) == [{"name": "cheap"}]


def scoped_widget(tenant, pk):
    """Resolve the target inside the caller's tenant."""
    return Widget.objects.filter(pk=pk)


def test_a_service_tool_advertises_its_target_lookup():
    """The model was told about ``name`` and ``price``, never about ``pk``, the row's key."""
    lookup = SelectorSpec(
        kind=SelectorKind.RETRIEVE,
        selector=scoped_widget,
        reads=Parameters.of(
            Parameter("tenant", "string", required=True, reflected=True),
            Parameter("pk", "integer", required=True, reflected=True),
        ),
    )
    toolset = SpecToolset(
        {"rename": _update_spec(instance_selector_spec=lookup)},
        pool_seeds=_tenant_seeds(),
    )

    schema = _input_schema_of(toolset, "rename")

    assert list(schema["properties"]) == ["pk", "name", "price"]
    assert schema["properties"]["pk"] == {"type": "integer"}
    assert schema["required"] == ["pk", "name", "price"]


def stamp_approval():
    """Approve, with nothing to say which row."""
    return {"approved": True}


@pytest.mark.parametrize(
    ("policy", "closed"),
    [(UnknownArguments.REJECT, False), (UnknownArguments.IGNORE, None)],
    ids=["reject", "ignore"],
)
def test_the_schema_closes_the_argument_set_only_where_the_call_does(policy, closed):
    """``additionalProperties: false`` holds only under ``REJECT``, not ``IGNORE``."""
    toolset = SpecToolset(
        {"list_widgets": list_spec(), "create_widgets": bulk_spec()}, unknown_arguments=policy
    )

    listed = _input_schema_of(toolset, "list_widgets")
    bulk = _input_schema_of(toolset, "create_widgets")

    assert listed.get("additionalProperties") is closed
    assert bulk.get("additionalProperties") is closed
    assert bulk["properties"]["items"]["items"].get("additionalProperties") is closed


def test_a_service_without_a_lookup_advertises_the_kernel_s_schema_as_it_is():
    """A service with no lookup or server-filled field has the kernel's schema byte for byte."""
    bare = ServiceSpec(service=stamp_approval, permissions=allow(), atomic=False)
    toolset = SpecToolset({"approve": bare, "make": create_spec()})

    assert _input_schema_of(toolset, "approve") == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert _input_schema_of(toolset, "make") == spec_input_schema(create_spec())


def widget_named(name):
    """Resolve the target by its current name."""
    return Widget.objects.filter(name=name)


def test_a_field_sharing_a_lookup_parameter_s_name_is_refused_naming_the_tool():
    """The kernel refuses a Validator field and a lookup read of one name as declared twice."""
    spec = _update_spec(
        instance_selector_spec=SelectorSpec(
            kind=SelectorKind.RETRIEVE,
            selector=widget_named,
            reads=Parameters.of(Parameter("name", "string", required=True)),
        )
    )

    with pytest.raises(
        ImproperlyConfigured,
        match=r"^SpecToolset tool 'rename': Parameters \['name'\] are declared by two sources\.",
    ):
        SpecToolset({"rename": spec})


def widget_or_newest(user, pk=None):
    """Resolve the target by primary key, or the newest widget when none is named."""
    rows = Widget.objects.filter(owner=user)
    return rows.filter(pk=pk) if pk is not None else rows.order_by("-pk")[:1]


class _PartialWidgetForm(forms.Form):
    """``WidgetInputForm`` with every field optional, as a partial update reads it."""

    name = forms.CharField(max_length=100, required=False)
    price = forms.IntegerField(min_value=0, required=False)


def test_a_lookup_that_can_run_without_arguments_requires_none():
    """A lookup read declared optional stays optional, so the tool requires nothing."""
    spec = _update_spec(
        validator=FormValidator(_PartialWidgetForm),
        instance_selector_spec=SelectorSpec(
            kind=SelectorKind.RETRIEVE,
            selector=widget_or_newest,
            reads=Parameters.of(Parameter("pk", "integer")),
        ),
    )

    schema = _input_schema_of(SpecToolset({"rename": spec}), "rename")

    assert list(schema["properties"]) == ["pk", "name", "price"]
    assert "required" not in schema


def widgets_priced_under(user, max_price):
    """The acting user's widgets at or below a price."""
    return Widget.objects.filter(owner=user, price__lte=max_price)


def reprice(collection, data):
    """Set one price on every widget in the set."""
    return collection.update(price=data["price"])


def test_a_collection_lookup_is_advertised_as_an_instance_lookup_is():
    """The kernel advertises a collection lookup's reads as it does an instance lookup's."""
    spec = ServiceSpec(
        service=reprice,
        validator=widget_input(),
        collection_selector_spec=SelectorSpec(
            kind=SelectorKind.LIST,
            selector=widgets_priced_under,
            reads=Parameters.of(Parameter("max_price", "integer", required=True)),
        ),
        permissions=allow(),
        atomic=False,
    )

    schema = _input_schema_of(SpecToolset({"reprice": spec}), "reprice")

    assert schema["properties"]["max_price"] == {"type": "integer"}
    assert schema["required"] == ["max_price", "name", "price"]


@pytest.mark.parametrize(
    ("lookups", "refusal"),
    [
        pytest.param(
            {
                "instance_selector_spec": by_pk(get_any_widget),
                "collection_selector_spec": SelectorSpec(
                    kind=SelectorKind.LIST, selector=widgets_priced_under
                ),
            },
            "both an instance and a collection selector",
            id="both-lookups",
        ),
        pytest.param(
            {
                "many": True,
                "instance_selector_spec": by_pk(get_any_widget),
            },
            "instance_selector_spec with many=True",
            id="lookup-beside-many",
        ),
    ],
)
def test_a_lookup_dispatch_never_calls_cannot_reach_the_toolset(lookups, refusal):
    """The kernel refuses, at build, a target lookup dispatch would never call."""
    with pytest.raises(ImproperlyConfigured, match=refusal):
        ServiceSpec(
            service=reprice,
            validator=widget_input(),
            permissions=allow(),
            atomic=False,
            **lookups,
        )


def stamp_widget(*, instance, note: str = ""):
    """Touch a widget. Only a spreading binding would hand it ``note``."""
    return instance


@pytest.mark.django_db(transaction=True)
async def test_a_validator_less_service_advertises_what_dispatch_admits():
    """Without a Validator, ``AUTO`` binds as ``BUNDLE``: ``REJECT`` admits the lookup's keys."""
    owner = await User.objects.acreate(username="owner")
    widget = await Widget.objects.acreate(owner=owner, name="old", price=1)
    spec = ServiceSpec(
        service=stamp_widget,
        instance_selector_spec=by_pk(get_any_widget),
        presenter=widget_presenter(),
        permissions=allow(),
        atomic=False,
    )
    toolset = SpecToolset({"stamp": spec})

    assert await _advertised(toolset, "stamp") == (["pk"], ["pk"])
    assert (await _call(toolset, "stamp", owner, {"pk": widget.pk}))["id"] == widget.pk
    with pytest.raises(ModelRetry, match=r"^note: Unknown argument\.$"):
        await _call(toolset, "stamp", owner, {"pk": widget.pk, "note": "seen"})


def names_in_range(user, low, high):
    """The names priced inside a range."""
    return [{"name": "cheap"}] if low <= 5 <= high else []


@pytest.mark.parametrize(
    ("spec", "args", "missing"),
    [
        (retrieve_spec(), {}, "`pk`"),
        (
            _name_list_spec(
                names_in_range,
                reads=Parameters.of(
                    Parameter("low", "integer", required=True),
                    Parameter("high", "integer", required=True),
                ),
            ),
            {"page": 1},
            "`high`, `low`",
        ),
    ],
    ids=["one", "several"],
)
async def test_a_selector_call_missing_a_required_argument_is_handed_back(spec, args, missing):
    """Handed back, not passed to the selector to raise ``TypeError`` out of the run."""
    toolset = SpecToolset({"tool": spec})

    with pytest.raises(ModelRetry) as raised:
        await _call(toolset, "tool", User(username="u"), args)

    assert str(raised.value) == f"Missing required argument(s): {missing}."


async def test_a_service_call_missing_its_lookup_is_handed_back():
    """Handed back, not passed to ``get_any_widget()`` to raise ``TypeError``."""
    toolset = SpecToolset({"rename": _update_spec()})

    with pytest.raises(ModelRetry) as raised:
        await _call(toolset, "rename", User(username="u"), {"name": "new", "price": 1})

    assert str(raised.value) == "Missing required argument(s): `pk`."


class _PkOrDecline(TypedDict):
    """``pk`` or ``UNSET``, for a service's own provider."""

    pk: int | UnsetType


def _pk_or_decline(view) -> _PkOrDecline:
    return _PkOrDecline(pk=UNSET)


def approve_widget(pk):
    """Approve, handed ``pk`` by its own provider or by the caller."""
    return {"approved": True}


async def test_a_key_one_seat_may_decline_is_still_required_by_another():
    """A provider's values reach only its own callable's seat, not the lookup's."""
    toolset = SpecToolset(
        {
            "approve": ServiceSpec(
                service=approve_widget,
                kwargs=_pk_or_decline,
                instance_selector_spec=by_pk(get_any_widget),
                permissions=allow(),
                atomic=False,
            )
        }
    )

    assert _input_schema_of(toolset, "approve")["required"] == ["pk"]
    with pytest.raises(ModelRetry) as raised:
        await _call(toolset, "approve", User(username="u"), {})
    assert str(raised.value) == "Missing required argument(s): `pk`."


def _untyped_nothing(view):
    """A service's own provider, with nothing declared about its keys."""
    return {}


async def test_a_read_an_untyped_provider_may_fill_is_still_required_by_another_seat():
    """An untyped provider fills no read in another seat, so ``pk`` stays advertised."""
    toolset = SpecToolset(
        {
            "approve": ServiceSpec(
                service=approve_widget,
                kwargs=_untyped_nothing,
                instance_selector_spec=by_pk(get_any_widget),
                permissions=allow(),
                atomic=False,
            )
        }
    )

    assert _input_schema_of(toolset, "approve")["required"] == ["pk"]
    with pytest.raises(ModelRetry) as raised:
        await _call(toolset, "approve", User(username="u"), {})
    assert str(raised.value) == "Missing required argument(s): `pk`."


@pytest.mark.django_db(transaction=True)
async def test_a_service_call_missing_its_lookup_and_its_fields_hears_about_each_in_turn():
    """The check before the call names the lookup's reads, not the Validator's fields."""
    owner = await User.objects.acreate(username="owner")
    widget = await Widget.objects.acreate(owner=owner, name="old", price=1)
    toolset = SpecToolset({"rename": _update_spec()})

    with pytest.raises(ModelRetry) as first:
        await _call(toolset, "rename", owner)
    with pytest.raises(ModelRetry) as second:
        await _call(toolset, "rename", owner, {"pk": widget.pk})

    assert str(first.value) == "Missing required argument(s): `pk`."
    assert str(second.value) == "name: This field is required.\nprice: This field is required."


@pytest.mark.django_db(transaction=True)
async def test_a_model_that_leaves_out_the_lookup_is_told_and_corrects_itself():
    """The retry lets a run reach the row on the second call, not abort on the first."""
    owner = await User.objects.acreate(username="owner")
    widget = await Widget.objects.acreate(owner=owner, name="old", price=1)
    agent = Agent(
        tool_calling_model(
            "rename",
            {"name": "new", "price": 1},
            retry_args={"pk": widget.pk, "name": "new", "price": 1},
        ),
        deps_type=AgentDeps,
        toolsets=[SpecToolset({"rename": _update_spec(presenter=widget_presenter())})],
    )

    result = await agent.run("go", deps=AgentDeps(user=owner))

    await widget.arefresh_from_db()
    assert result.output == "done"
    assert widget.name == "new"


# --- a read beside a provider ------------------------------------------------


class _Status(TypedDict):
    status: str


def _status_from_the_query(filed_as: str = "open") -> _Status:
    """The status the caller asked for, read by name off the declared read."""
    return _Status(status=filed_as)


def names_filed_under(user, *, status: str):
    """The names filed under one status."""
    filed = (("draft", "open"), ("signed", "closed"))
    return [{"name": name} for name, under in filed if under == status]


@pytest.mark.parametrize(
    ("args", "rows"),
    [({"filed_as": "closed"}, [{"name": "signed"}]), ({}, [{"name": "draft"}])],
    ids=["sent", "left-out"],
)
async def test_a_query_param_a_typed_provider_hands_the_selector_is_served(args, rows):
    """A ``kwargs=`` provider can route a caller's argument to a selector under another name."""
    toolset = SpecToolset(
        reading(
            {"filed": _name_list_spec(names_filed_under, kwargs=_status_from_the_query)},
            {"filed": [Parameter("filed_as", "string")]},
        )
    )

    schema = _input_schema_of(toolset, "filed")
    result = await _call(toolset, "filed", User(username="u"), args)

    assert "status" not in schema["properties"]
    assert _rows(result) == rows


def _status_from_itself(status: str = "open") -> _Status:
    """A provider filling the very name it reads."""
    return _Status(status=status)


async def test_a_provider_filling_the_name_it_reads_is_not_handed_the_model_s_value():
    """A model's ``status`` is dropped as a key the provider owns, so its own default is served."""
    toolset = SpecToolset(
        reading(
            {"filed": _name_list_spec(names_filed_under, kwargs=_status_from_itself)},
            {"filed": [Parameter("status", "string")]},
        )
    )

    result = await _call(toolset, "filed", User(username="u"), {"status": "closed"})

    assert _rows(result) == [{"name": "draft"}]


def test_a_read_the_provider_fills_is_not_advertised():
    """The schema matches: ``status`` is server-owned, so the model is not offered it."""
    toolset = SpecToolset(
        reading(
            {"filed": _name_list_spec(names_filed_under, kwargs=_status_from_itself)},
            {"filed": [Parameter("status", "string")]},
        )
    )

    assert "status" not in _input_schema_of(toolset, "filed")["properties"]


# --- the pagination envelope --------------------------------------------------
#
# Every list-selector result is a page. The input contract said so all along --
# ``page`` and ``limit`` were advertised on every list tool -- while the payload
# was a bare slice with no total and no clamp. These are about the half that was
# missing: what the model is told about the rows it was *not* given.


@pytest.mark.django_db(transaction=True)
async def test_a_list_result_is_a_page_not_a_bare_list():
    user = await User.objects.acreate(username="u")
    await Widget.objects.acreate(name="a", price=1, owner=user)

    widget = await Widget.objects.aget(name="a")
    result = await _call(SpecToolset({"list_widgets": list_spec()}), "list_widgets", user)

    assert result == {
        "items": [{"id": widget.pk, "name": "a", "price": 1}],
        "page": 1,
        "totalPages": 1,
        "hasNext": False,
    }


@pytest.mark.django_db
def test_a_truncated_page_says_that_it_was_truncated():
    """Reporting ``limit`` rows with nothing about the rest invites a wrong total."""
    user = User.objects.create(username="u")
    for n in range(5):
        Widget.objects.create(name=f"w{n}", price=n, owner=user)

    result = _dispatch(list_spec(), user, {"limit": 2})

    assert len(_rows(result)) == 2
    assert result["page"] == 1
    assert result["totalPages"] == 3
    assert result["hasNext"] is True


@pytest.mark.django_db
def test_the_last_page_reports_no_next():
    user = User.objects.create(username="u")
    for n in range(5):
        Widget.objects.create(name=f"w{n}", price=n, owner=user)

    result = _dispatch(list_spec(), user, {"limit": 2, "page": 3})

    assert len(_rows(result)) == 1
    assert result["page"] == 3
    assert result["hasNext"] is False


@pytest.mark.django_db
def test_a_page_past_the_end_is_clamped_and_says_which_page_it_served():
    """The kernel clamps rather than serving an empty page at a huge OFFSET."""
    user = User.objects.create(username="u")
    for n in range(3):
        Widget.objects.create(name=f"w{n}", price=n, owner=user)

    result = _dispatch(list_spec(), user, {"limit": 2, "page": 99})

    assert result["page"] == 2
    assert result["totalPages"] == 2
    assert result["hasNext"] is False


@pytest.mark.django_db
def test_an_empty_collection_is_one_empty_page():
    """``page`` is 1-based, so reporting zero pages would describe the page served as absent."""
    user = User.objects.create(username="u")

    result = _dispatch(list_spec(), user, {})

    assert result == {"items": [], "page": 1, "totalPages": 1, "hasNext": False}


@pytest.mark.django_db
def test_the_envelope_carries_projected_rows_and_is_not_itself_projected():
    """The projection lands on the rows; envelope keys belong to no presenter."""
    user = User.objects.create(username="u")
    widget = Widget.objects.create(name="Sprocket", price=100, owner=user)

    result = _dispatch(agent_list_spec(), user)

    assert _rows(result) == [{"id": widget.pk, "name": "Sprocket", "status": "In stock"}]
    assert result["page"] == 1


@pytest.mark.django_db
def test_the_byte_ceiling_is_measured_on_the_envelope():
    """What is sent is what is measured."""
    user = User.objects.create(username="u")
    Widget.objects.create(name="a", price=1, owner=user)

    with pytest.raises(ToolFailed) as caught:
        _dispatch(list_spec(), user, {}, max_result_bytes=40)

    assert "40 byte ceiling" in caught.value.message


async def test_the_pagination_instruction_teaches_the_envelope():
    """Told to send ``limit``, a model reads ``items`` as the whole collection."""
    instructions = await SpecToolset({"list": list_spec()}).get_instructions(None)

    assert instructions is not None
    assert '"items"' in instructions
    assert "hasNext" in instructions


async def test_the_page_argument_no_longer_claims_to_require_limit():
    """Alone, it did nothing before: an omitted ``limit`` skipped the slice."""
    tools = await SpecToolset({"list": list_spec()}).get_tools(None)
    props = tools["list"].tool_def.parameters_json_schema["properties"]

    assert "requires" not in props["page"]["description"]
    assert str(DEFAULT_PAGE_SIZE) in props["limit"]["description"]


def _stated_default(description: str) -> int:
    """The page size a ``limit`` description says an omitted ``limit`` gets."""
    match = re.search(r"Defaults to (\d+);", description)
    assert match is not None, description
    return int(match.group(1))


@pytest.mark.django_db(transaction=True)
async def test_the_limit_description_states_the_default_a_call_is_served():
    """Read off the schema, then checked against what a call with no ``limit`` gets."""
    user = await User.objects.acreate(username="u")
    for n in range(5):
        await Widget.objects.acreate(name=f"w{n}", price=n, owner=user)
    toolset = SpecToolset({"list": list_spec()}, max_page_size=3)
    tools = await toolset.get_tools(ctx_for(user))
    limit = tools["list"].tool_def.parameters_json_schema["properties"]["limit"]

    served = _rows(await _call(toolset, "list", user))

    assert limit["maximum"] == 3
    assert _stated_default(limit["description"]) == len(served) == 3


async def test_a_ceiling_above_the_default_page_size_leaves_the_stated_default_alone():
    """The served default is the smaller of the two, not the ceiling."""
    ceiling = DEFAULT_PAGE_SIZE * 5
    toolset = SpecToolset({"list": list_spec()}, max_page_size=ceiling)

    tools = await toolset.get_tools(None)
    limit = tools["list"].tool_def.parameters_json_schema["properties"]["limit"]
    instructions = await toolset.get_instructions(None)

    assert limit["maximum"] == ceiling
    assert _stated_default(limit["description"]) == DEFAULT_PAGE_SIZE
    assert f"default {DEFAULT_PAGE_SIZE})" in instructions
    assert str(ceiling) not in instructions


async def test_the_pagination_instruction_states_the_default_a_ceiling_serves():
    """The conventions block carried the same module-level number."""
    instructions = await SpecToolset({"list": list_spec()}, max_page_size=3).get_instructions(None)

    assert instructions is not None
    assert "`limit` (items per page, default 3)" in instructions
    assert f"default {DEFAULT_PAGE_SIZE}" not in instructions


async def test_a_step_leaving_a_tool_out_states_the_same_default():
    """The per-step block is derived on its own path, so it needs the ceiling too."""
    toolset = SpecToolset(
        {"list": list_spec(), "approve": _conditioned_spec(lambda: False)}, max_page_size=3
    )

    instructions = await toolset.get_instructions(ctx_for(User(username="u")))

    assert "`approve`: The books are closed." in instructions
    assert "`limit` (items per page, default 3)" in instructions


# --- ToolDefinition.return_schema ---------------------------------------------


async def test_a_list_tool_advertises_the_envelope_it_returns():
    tools = await SpecToolset({"list": list_spec()}).get_tools(None)
    return_schema = tools["list"].tool_def.return_schema

    assert return_schema is not None
    assert set(return_schema["properties"]) == {"items", "page", "totalPages", "hasNext"}
    assert set(return_schema["properties"]["items"]["items"]["properties"]) == {
        "id",
        "name",
        "price",
    }


@pytest.mark.django_db(transaction=True)
async def test_the_return_schema_and_the_payload_agree_for_a_list_tool():
    """The two halves generated from one spec, compared against each other."""
    user = await User.objects.acreate(username="u")
    await Widget.objects.acreate(name="a", price=1, owner=user)
    toolset = SpecToolset({"list_widgets": list_spec()})

    tools = await toolset.get_tools(None)
    payload = await _call(toolset, "list_widgets", user)
    return_schema = tools["list_widgets"].tool_def.return_schema

    assert return_schema is not None
    assert set(payload) == set(return_schema["properties"])
    assert set(payload["items"][0]) == set(
        return_schema["properties"]["items"]["items"]["properties"]
    )


async def test_a_retrieve_tool_advertises_the_bare_item():
    """``paginate`` asks whether this toolset wraps the result; only a list selector does."""
    tools = await SpecToolset({"get": retrieve_spec()}).get_tools(None)
    return_schema = tools["get"].tool_def.return_schema

    assert return_schema is not None
    assert set(return_schema["properties"]) == {"id", "name", "price"}


@pytest.mark.parametrize(
    ("allow_none", "root_type"), [(True, ["object", "null"]), (False, "object")]
)
async def test_a_retrieve_tool_says_whether_it_can_return_null(allow_none, root_type):
    """An ``allow_none`` retrieve returns ``None`` when empty; an object schema denies it."""
    tools = await SpecToolset({"get": retrieve_spec(allow_none=allow_none)}).get_tools(None)
    return_schema = tools["get"].tool_def.return_schema

    assert return_schema is not None
    assert return_schema["type"] == root_type


@dataclass
class _OptionalNoteInput:
    name: str
    note: str | None = None


def _optional_note_service(name, note=None):
    """Take an optional note."""
    return {"id": 1}


def _nullable_read_spec() -> SelectorSpec:
    return retrieve_spec(reads=Parameters.of(_PK, Parameter("note", "string", nullable=True)))


def _optional_field_spec() -> ServiceSpec:
    return ServiceSpec(
        service=_optional_note_service,
        validator=django_service_specs.DataclassValidator(_OptionalNoteInput),
        permissions=allow(),
        atomic=False,
    )


@pytest.mark.parametrize("spec", [_nullable_read_spec, _optional_field_spec])
async def test_an_input_that_admits_none_is_one_node_listing_null_beside_its_type(spec):
    """A nullable read is one schema node whose ``type`` lists ``null`` beside its own type."""
    tools = await SpecToolset({"get": spec()}).get_tools(None)
    properties = tools["get"].tool_def.parameters_json_schema["properties"]

    assert properties["note"]["type"] == ["string", "null"]
    assert "anyOf" not in properties["note"]


def archive_widget():
    """Archive a widget, returning the row as it stood."""
    return {"id": 1, "name": "old", "price": 1}


def live_widget(*, result):
    """The output re-read: the row just archived is no longer live."""
    return None


def touch_widgets():
    """A service with nothing to return."""
    return None


async def _returned_against_its_schema(spec: ServiceSpec) -> tuple[Any, Any]:
    """What one call to ``spec``'s tool hands the model, and the root type it was promised."""
    toolset = SpecToolset({"op": spec})
    tools = await toolset.get_tools(None)
    payload = await _call(toolset, "op", User(username="u"))
    return_schema = tools["op"].tool_def.return_schema
    assert return_schema is not None
    return payload, return_schema["type"]


async def test_a_service_whose_reread_can_find_no_row_admits_null():
    """A re-read filtering the row out hands the model ``None``, which required fields refuse."""
    spec = ServiceSpec(
        service=archive_widget,
        atomic=False,
        output_selector_spec=SelectorSpec(
            kind=SelectorKind.RETRIEVE, selector=live_widget, presenter=widget_presenter()
        ),
        permissions=allow(),
    )

    assert await _returned_against_its_schema(spec) == (None, ["object", "null"])


async def test_a_service_declaring_allow_none_admits_null():
    """With nothing to re-read, dispatch presents the service's own return, a ``None`` included."""
    spec = ServiceSpec(
        service=touch_widgets,
        atomic=False,
        allow_none=True,
        presenter=widget_presenter(),
        permissions=allow(),
    )

    assert await _returned_against_its_schema(spec) == (None, ["object", "null"])


async def test_an_undeclared_none_is_handed_to_the_model_against_an_object_schema():
    """Without ``allow_none``, a ``None`` return is still presented to the model."""
    spec = ServiceSpec(
        service=touch_widgets,
        atomic=False,
        presenter=widget_presenter(),
        permissions=allow(),
    )

    assert await _returned_against_its_schema(spec) == (None, "object")


def _reread_rows(*, result):
    """The output re-read of a write that returns its rows: the rows as returned."""
    return result


def _making(output: SelectorSpec, **kwargs: Any) -> ServiceSpec:
    """``create_spec`` presenting through the output re-read ``output``."""
    return ServiceSpec(
        service=create_widget,
        validator=widget_input(),
        output_selector_spec=output,
        permissions=allow(),
        **kwargs,
    )


@pytest.mark.parametrize(
    ("spec", "root_type"),
    [
        pytest.param(create_spec(), "object", id="declares-nothing"),
        pytest.param(
            _making(
                SelectorSpec(
                    kind=SelectorKind.LIST, selector=_reread_rows, presenter=widget_presenter()
                ),
                allow_none=True,
            ),
            "object",
            id="list-reread",
        ),
        pytest.param(
            ServiceSpec(
                service=touch_widgets,
                atomic=False,
                presenter=widget_presenter(),
                returns_many=True,
                allow_none=True,
                permissions=allow(),
            ),
            "object",
            id="returns-many",
        ),
        pytest.param(bulk_spec(allow_none=True), "object", id="many"),
    ],
)
def test_a_service_admits_null_only_where_dispatch_may_present_it(spec, root_type):
    """The reading is the kernel's ``can_present_nothing``, the answer every route states."""
    return_schema = _return_schema(spec)

    assert return_schema is not None
    assert return_schema["type"] == root_type


async def test_a_service_tool_reads_its_output_shape_off_its_output_re_read():
    """A ``ServiceSpec`` may keep the output shape on its ``output_selector_spec``."""
    spec = _making(
        SelectorSpec(kind=SelectorKind.RETRIEVE, selector=get_widget, presenter=widget_presenter())
    )
    tools = await SpecToolset({"make": spec}).get_tools(None)
    return_schema = tools["make"].tool_def.return_schema

    assert return_schema is not None
    assert set(return_schema["properties"]) == {"id", "name", "price"}


_TOUCHED = [{"id": 1, "name": "old", "price": 1}, {"id": 2, "name": "new", "price": 2}]


def touch_every_widget():
    """Touch the widgets, returning the rows touched."""
    return _TOUCHED


def _presenting_a_list(service: Any) -> ServiceSpec:
    """``service`` presenting its own return as a list of rows, with nothing to re-read."""
    return ServiceSpec(
        service=service,
        atomic=False,
        presenter=widget_presenter(),
        returns_many=True,
        permissions=allow(),
    )


async def test_a_service_whose_output_is_a_list_is_its_rows_under_items():
    """The toolset paginates list *selectors* only, so a service's list carries no page envelope."""
    assert await _returned_against_its_schema(_presenting_a_list(touch_every_widget)) == (
        {"items": _TOUCHED},
        "object",
    )


async def test_a_list_that_presents_nothing_is_no_rows():
    """A ``returns_many`` service with ``allow_none`` may return ``None``."""
    spec = ServiceSpec(
        service=touch_widgets,
        atomic=False,
        presenter=widget_presenter(),
        returns_many=True,
        allow_none=True,
        permissions=allow(),
    )

    assert await _returned_against_its_schema(spec) == ({"items": []}, "object")


async def test_a_single_row_that_presents_nothing_is_still_none():
    """Only a list-shaped spec's ``None`` becomes no rows; a single row keeps ``None``."""
    spec = ServiceSpec(
        service=touch_widgets,
        atomic=False,
        allow_none=True,
        presenter=widget_presenter(),
        permissions=allow(),
    )

    assert await _returned_against_its_schema(spec) == (None, ["object", "null"])


def test_a_service_with_no_presenter_that_returns_nothing_returns_none():
    """A service with no presenter has no output schema, so its ``None`` passes through."""
    assert _dispatch(ServiceSpec(service=lambda: None, atomic=False), object(), {}) is None


def test_a_service_with_no_presenter_returning_a_tuple_is_its_rows_under_items():
    """The value decides, as in django-spec-mcp: a tuple is wrapped as a list is."""

    def rows():
        return ({"a": 1},)

    spec = ServiceSpec(service=rows, atomic=False)

    assert _dispatch(spec, object(), {}) == {"items": [{"a": 1}]}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The kernel's dispatch hands a returns_many service's return to the presenter "
        "when it is no list, where it should refuse it."
    ),
)
async def test_a_list_output_with_nothing_to_reread_refuses_a_single_row():
    """Declared ``returns_many``, a one-row return is a configuration error."""
    with pytest.raises(ImproperlyConfigured, match=r"archive_widget returned dict"):
        await _call(SpecToolset({"op": _presenting_a_list(archive_widget)}), "op", None)


async def test_an_output_fragment_annotates_the_object_a_list_is_wrapped_in():
    """The fragment merges after wrapping, so its keys annotate the object handed over."""
    spec = replace(
        _presenting_a_list(touch_every_widget),
        metadata={"json_schema": {"output": {"description": "X", "minItems": 1}}},
    )

    tools = await SpecToolset({"op": spec}).get_tools(None)
    return_schema = tools["op"].tool_def.return_schema

    assert return_schema is not None
    assert return_schema["description"] == "X"
    assert return_schema["properties"]["items"]["type"] == "array"


def test_a_spec_with_no_presenter_advertises_nothing():
    """``None`` is correct: a fabricated shape would claim what the payload never honours."""
    spec = SelectorSpec(kind=SelectorKind.LIST, selector=list_widgets, permissions=allow())

    assert _return_schema(spec) is None
    assert _build_tool_def("list", spec).return_schema is None


async def test_the_return_schema_is_projected_like_the_payload():
    """A schema advertising a field the render drops is worse than none."""
    toolset = SpecToolset({"widgets": agent_list_spec()})

    tools = await toolset.get_tools(None)
    item = tools["widgets"].tool_def.return_schema["properties"]["items"]["items"]

    assert "price" not in item["properties"]
    assert item["properties"]["id"]["description"] == "Widget handle."


async def test_an_unlabelled_handle_gets_this_transports_own_wording():
    """The kernel supplies no fallback on purpose: only the transport knows its audience."""
    spec = retrieve_spec(
        presenter=RowPresenter(
            OutputField("id", "integer", marking=FieldMarking.handle()),
            OutputField("name", "string"),
        )
    )
    tools = await SpecToolset({"get": spec}).get_tools(None)

    description = tools["get"].tool_def.return_schema["properties"]["id"]["description"]
    assert description == _DEFAULTS.handle_field_description


async def test_the_return_schema_is_not_pushed_at_the_model_by_default():
    """Populating it costs nothing; putting it on the wire costs context every turn."""
    tools = await SpecToolset({"list": list_spec()}).get_tools(None)

    assert tools["list"].tool_def.include_return_schema is None


async def test_a_consumer_can_opt_into_sending_the_return_schema():
    """The opt-in must reach a schema that exists: the wrapper keeps only what the def carries."""
    toolset = SpecToolset({"list": list_spec()}).include_return_schemas()
    ctx = RunContext(deps=AgentDeps(user=None), model=TestModel(), usage=RunUsage())

    tools = await toolset.get_tools(ctx)

    assert tools["list"].tool_def.include_return_schema is True
    assert tools["list"].tool_def.return_schema is not None


# --- affordances: the return schema declares the key the render adds ----------


def _order_row() -> RowPresenter:
    """An order as its ``number``."""
    return RowPresenter(OutputField("number", "string"))


#: The operation each rendered order reports on. Two conditions, so the codes the
#: schema enumerates have an order to keep; the first is unmet, so every answer
#: rendered is a refusal carrying ``code`` and ``reason`` -- the widest answer the
#: schema has to describe. Callable conditions, which need no database and are the
#: only kind a mapping row can be answered for.
_CANCEL_ORDER = ServiceSpec(
    service=lambda **_: None,
    atomic=False,
    affordances=[
        Affordance(code="books_closed", reason="The books are closed.", when=lambda **_: False),
        Affordance(code="already_shipped", reason="The order shipped.", when=lambda **_: True),
    ],
)


def _get_order(**_: Any) -> dict[str, str]:
    """Fetch one order."""
    return {"number": "A-1"}


def _list_orders(**_: Any) -> list[dict[str, str]]:
    """List orders."""
    return [{"number": "A-1"}, {"number": "A-2"}]


def _place_order(**_: Any) -> dict[str, str]:
    """Place an order."""
    return {"number": "A-1"}


def _order_selector_spec(kind: SelectorKind) -> SelectorSpec:
    return SelectorSpec(
        kind=kind,
        selector=_list_orders if kind is SelectorKind.LIST else _get_order,
        presenter=_order_row(),
        affordances={"cancel": _CANCEL_ORDER},
        permissions=allow(),
    )


def _order_service_spec(
    *,
    rendered: dict[str, ServiceSpec] | None = None,
    own: list[Affordance] | None = None,
) -> ServiceSpec:
    """A service whose output renders through a selector declaring ``rendered``."""
    return ServiceSpec(
        service=_place_order,
        atomic=False,
        affordances=own,
        output_selector_spec=SelectorSpec(
            kind=SelectorKind.RETRIEVE,
            selector=lambda result: result,
            presenter=_order_row(),
            affordances=rendered,
        ),
        permissions=allow(),
    )


#: Each shape a rendered item takes: the spec, where the item sits in the return
#: schema, and where it sits in the payload a call returns.
_AFFORDANCE_SHAPES = [
    pytest.param(
        lambda: _order_selector_spec(SelectorKind.RETRIEVE),
        lambda schema: schema,
        lambda payload: [payload],
        id="retrieve selector",
    ),
    pytest.param(
        lambda: _order_selector_spec(SelectorKind.LIST),
        lambda schema: schema["properties"]["items"]["items"],
        lambda payload: payload["items"],
        id="paginated list selector",
    ),
    pytest.param(
        lambda: _order_service_spec(rendered={"cancel": _CANCEL_ORDER}),
        lambda schema: schema,
        lambda payload: [payload],
        id="service rendering through its output selector",
    ),
]


@pytest.mark.parametrize(("make_spec", "schema_item", "payload_items"), _AFFORDANCE_SHAPES)
async def test_declared_affordances_are_advertised_on_every_item(
    make_spec, schema_item, payload_items
):
    """Presented items carry ``affordances``, which the advertised schema never said."""
    tools = await SpecToolset({"orders": make_spec()}).get_tools(None)

    item = schema_item(tools["orders"].tool_def.return_schema)

    assert "affordances" in item["required"]
    answers = item["properties"]["affordances"]
    assert answers["required"] == ["cancel"]
    cancel = answers["properties"]["cancel"]
    assert cancel["required"] == ["available"]
    assert cancel["properties"]["code"]["enum"] == ["books_closed", "already_shipped"]


@pytest.mark.parametrize(("make_spec", "schema_item", "payload_items"), _AFFORDANCE_SHAPES)
async def test_the_return_schema_describes_the_affordances_a_call_returns(
    make_spec, schema_item, payload_items
):
    """Both halves come from one declaration and are compared through a real call."""
    toolset = SpecToolset({"orders": make_spec()})
    tools = await toolset.get_tools(None)
    item = schema_item(tools["orders"].tool_def.return_schema)

    rendered = payload_items(await _call(toolset, "orders", User(username="u")))

    assert rendered
    for row in rendered:
        assert set(row) == set(item["properties"])
        assert set(item["required"]) <= set(row)
        answers_schema = item["properties"]["affordances"]
        assert set(row["affordances"]) == set(answers_schema["properties"])
        answer = row["affordances"]["cancel"]
        answer_schema = answers_schema["properties"]["cancel"]
        assert answer == {
            "available": False,
            "code": "books_closed",
            "reason": "The books are closed.",
        }
        assert set(answer) <= set(answer_schema["properties"])
        assert answer["code"] in answer_schema["properties"]["code"]["enum"]


async def test_a_services_own_affordances_are_not_what_it_renders():
    """``ServiceSpec.affordances`` are checked before the service runs and never rendered."""
    spec = _order_service_spec(
        own=[Affordance(code="books_closed", reason="The books are closed.", when=lambda **_: True)]
    )
    toolset = SpecToolset({"orders": spec})
    tools = await toolset.get_tools(ctx_for(User(username="u")))

    payload = await _call(toolset, "orders", User(username="u"))

    assert "affordances" not in tools["orders"].tool_def.return_schema["properties"]
    assert "affordances" not in payload


@pytest.mark.parametrize(
    ("make_spec", "paginate"),
    [
        pytest.param(list_spec, True, id="list selector"),
        pytest.param(retrieve_spec, False, id="retrieve selector"),
        pytest.param(create_spec, False, id="service"),
        pytest.param(agent_list_spec, True, id="projected list"),
    ],
)
async def test_a_spec_declaring_no_affordances_keeps_its_return_schema_byte_for_byte(
    make_spec, paginate
):
    """Compared as JSON text, so key order counts: a spec declaring none must not change."""
    spec = make_spec()
    tools = await SpecToolset({"t": spec}).get_tools(None)

    before = spec_output_schema(
        spec,
        paginate=paginate,
        projection=audience_projection_for_spec(spec),
        handle_description=_DEFAULTS.handle_field_description,
    )

    assert json.dumps(tools["t"].tool_def.return_schema) == json.dumps(before)


# --- a refused call names the rule that refused it ----------------------------
#
# The kernel raises ``ActionUnavailable(reason, code=...)`` when an affordance
# condition is not met. ``ToolFailed`` takes a message and nothing else, so the
# sentence is the only place the code can reach the model -- and the code is what
# ties the refusal to the ``affordances`` answer the model may already have read.
#
# A condition answered without a row also leaves its tool out of the catalog
# while it is unmet, so a run normally never reaches these refusals through a
# listing. What does reach them is a call made against a listing that went stale
# -- the condition flipped after the step was offered the tool -- and a
# condition on the row, which no listing asks. So these call the tool directly,
# the way a stale entry arrives, rather than through ``get_tools``.


def _refusing_service(**_: Any) -> None:
    """Close the books. Never reached: its one condition is unmet."""


def _refused_spec() -> ServiceSpec:
    return ServiceSpec(
        service=_refusing_service,
        atomic=False,
        affordances=[
            Affordance(code="books_closed", reason="The books are closed.", when=lambda: False)
        ],
        permissions=allow(),
    )


async def test_a_refused_call_fails_with_its_reason_and_its_code():
    """Through ``call_tool``, the path a run takes."""
    toolset = SpecToolset({"close_books": _refused_spec()})

    with pytest.raises(ToolFailed) as caught:
        await toolset.call_tool("close_books", {}, ctx_for(User(username="u")), None)

    assert caught.value.message == "The books are closed. (code: books_closed)"
    cause = caught.value.__cause__
    assert isinstance(cause, ActionUnavailable)
    assert cause.code == "books_closed"
    assert cause.message == "The books are closed."


async def test_a_refusal_names_the_code_its_rows_advertised():
    """A row's affordance code is the code that operation's refusal carries."""
    toolset = SpecToolset(
        {
            "orders": _order_selector_spec(SelectorKind.RETRIEVE),
            "cancel": replace(_CANCEL_ORDER, permissions=allow()),
        },
        descriptions={"cancel": "Cancel an order."},
    )
    row = await _call(toolset, "orders", User(username="u"))
    advertised = row["affordances"]["cancel"]

    with pytest.raises(ToolFailed) as caught:
        await toolset.call_tool("cancel", {}, ctx_for(User(username="u")), None)

    assert caught.value.message == f"{advertised['reason']} (code: {advertised['code']})"


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(ServiceConflict("That slot is already booked."), id="ServiceConflict"),
        pytest.param(ServiceError("That slot is already booked."), id="ServiceError"),
    ],
)
def test_a_business_error_without_a_code_keeps_its_message_unchanged(exc):
    """``ServiceConflict`` is a child of ``ActionUnavailable`` and matches the same branch."""

    def refuse(**_):
        """Refuse."""
        raise exc

    with pytest.raises(ToolFailed) as caught:
        _dispatch(ServiceSpec(service=refuse, atomic=False), object(), {})

    assert caught.value.message == "That slot is already booked."
    assert "(code:" not in caught.value.message
    assert caught.value.__cause__ is exc


@pytest.mark.parametrize(
    "registered",
    [
        pytest.param(ActionUnavailable, id="the refusal's own class"),
        pytest.param(ServiceError, id="a base class it inherits"),
    ],
)
def test_the_exception_map_still_wins_over_the_code_suffix(registered):
    """The consumer's map runs before any built-in arm, where a program reads ``.code``."""
    result = _dispatch(
        _refused_spec(),
        object(),
        toolset_kwargs={
            "exception_map": {registered: lambda exc: {"refused": exc.code, "why": exc.message}}
        },
    )

    assert result == {"refused": "books_closed", "why": "The books are closed."}


def test_translate_exception_sees_the_code_before_the_default_renders_it():
    """The override is the other door to the same place, and it can read the run."""

    class ReadingTheCode(SpecToolset):
        def translate_exception(self, exc, *, ctx):
            if isinstance(exc, ActionUnavailable) and ctx.deps.user == "auditor":
                return lambda e: {"refused": e.code}
            return super().translate_exception(exc, ctx=ctx)

    spec = _refused_spec()
    toolset = ReadingTheCode({"t": spec})

    assert toolset._call_spec(spec, "auditor", {}, ctx=ctx_for("auditor"), label="t") == {
        "refused": "books_closed"
    }
    with pytest.raises(ToolFailed) as caught:
        toolset._call_spec(spec, "clerk", {}, ctx=ctx_for("clerk"), label="t")
    assert caught.value.message == "The books are closed. (code: books_closed)"


# --- A filter's schema ----------------------------------------------------------


class _RatingFilter(django_filters.NumberFilter):
    """A project's own filter type, which no rule anywhere describes."""


class _RatedFilterSet(django_filters.FilterSet):
    rating = _RatingFilter(field_name="id")

    class Meta:
        model = Widget
        fields = []


async def test_a_filter_is_described_as_the_kernel_derives_it():
    """The kernel derives a filter's node from its filter set, with no schema registry."""
    spec = django_service_specs.SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_widgets,
        filter_set=_RatedFilterSet,
        permissions=[django_service_specs.Unrestricted()],
    )

    tools = await SpecToolset({"list": spec}).get_tools(None)

    rating = tools["list"].tool_def.parameters_json_schema["properties"]["rating"]
    assert rating == spec_input_schema(spec)["properties"]["rating"]
    assert rating["type"] == "string"
    assert rating["format"] == "decimal"


def _thread_naming_spec() -> ServiceSpec:
    """A spec whose result is the name of the thread it was dispatched on."""

    def _service(**_: Any) -> dict[str, str]:
        """Report the thread this dispatch ran on."""
        time.sleep(0.05)
        return {"thread": threading.current_thread().name}

    return ServiceSpec(service=_service, permissions=allow())


async def _call_concurrently(toolset: SpecToolset, *, times: int) -> list[str]:
    """Run ``times`` tool calls under one ``gather``, returning their threads."""
    ctx = ctx_for(User(username="worker"))
    tools = await toolset.get_tools(ctx)
    results = await asyncio.gather(
        *(toolset.call_tool("echo", {}, ctx, tools["echo"]) for _ in range(times))
    )
    return [r["thread"] for r in results]


# ---------- the dispatch thread ----------


@pytest.mark.django_db(transaction=True)
async def test_concurrent_tool_calls_share_one_thread_by_default() -> None:
    """asgiref's default, and the reason the knob exists rather than a flip."""
    toolset = SpecToolset({"echo": _thread_naming_spec()})

    names = await _call_concurrently(toolset, times=3)

    assert len(set(names)) == 1


@pytest.mark.django_db(transaction=True)
async def test_the_dispatch_thread_is_the_callers_to_choose() -> None:
    """With ``thread_sensitive=False`` and an executor, calls actually fan out."""
    with ThreadPoolExecutor(max_workers=3) as pool:
        toolset = SpecToolset(
            {"echo": _thread_naming_spec()}, thread_sensitive=False, executor=pool
        )

        names = await _call_concurrently(toolset, times=3)

    assert len(set(names)) == 3


def _dispatch_thread_state() -> tuple[int, bool]:
    """This thread's ident, and whether it is holding an open default connection."""
    return threading.get_ident(), connections["default"].connection is not None


def _thread_recording_toolset() -> tuple[SpecToolset, dict[str, int]]:
    """A one-tool toolset whose selector records, in the dict beside it, its thread."""
    dispatched_on: dict[str, int] = {}

    def _list_recording_its_thread(user: Any) -> Any:
        """List every widget, recording which thread the dispatch ran on."""
        del user
        dispatched_on["thread"] = threading.get_ident()
        return Widget.objects.all()

    toolset = SpecToolset(
        {
            "widgets": SelectorSpec(
                kind=SelectorKind.LIST,
                selector=_list_recording_its_thread,
                presenter=widget_presenter(),
                permissions=allow(),
            )
        }
    )
    return toolset, dispatched_on


def _assert_a_close_is_observable() -> None:
    """Django's SQLite backend ignores ``close()`` on an in-memory database."""
    assert not connection.is_in_memory_db(), (
        "this test needs a database whose connection can actually be closed; "
        "see DATABASES['default']['TEST']['NAME'] in tests/conftest_settings.py"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("conn_max_age", [0, 60], ids=["no-reuse", "persistent-connections"])
async def test_a_dispatch_closes_the_connection_it_opened_on_the_shared_thread(
    conn_max_age: int,
) -> None:
    """A dispatch must not leave a database connection open behind it."""
    _assert_a_close_is_observable()

    toolset, dispatched_on = _thread_recording_toolset()
    ctx = ctx_for(User(username="u"))
    tools = await toolset.get_tools(ctx)

    settings_dict = connections.settings["default"]
    original_max_age = settings_dict["CONN_MAX_AGE"]
    settings_dict["CONN_MAX_AGE"] = conn_max_age
    try:
        await sync_to_async(connections.close_all, thread_sensitive=True)()
        assert (await sync_to_async(_dispatch_thread_state, thread_sensitive=True)())[1] is False

        await toolset.call_tool("widgets", {}, ctx, tools["widgets"])

        ident, still_open = await sync_to_async(_dispatch_thread_state, thread_sensitive=True)()
    finally:
        settings_dict["CONN_MAX_AGE"] = original_max_age
    assert ident == dispatched_on["thread"], "the probe did not land on the dispatch thread"
    assert still_open is False, (
        "the dispatch left an open database connection on asgiref's shared "
        "sync_to_async thread; off HTTP there is no request boundary to close it"
    )


@pytest.mark.django_db
def test_a_dispatch_leaves_a_connection_it_did_not_open_alone() -> None:
    """The other half, and the one an unconditional cleanup gets wrong."""
    _assert_a_close_is_observable()
    connection.ensure_connection()
    assert connection.in_atomic_block

    toolset, dispatched_on = _thread_recording_toolset()
    ctx = ctx_for(User(username="u"))

    async def _one_call() -> None:
        tools = await toolset.get_tools(ctx)
        await toolset.call_tool("widgets", {}, ctx, tools["widgets"])

    async_to_sync(_one_call)()

    assert dispatched_on["thread"] == threading.get_ident(), (
        "asgiref did not route the dispatch back onto the calling thread, so "
        "this test is no longer about the case it names"
    )
    assert connection.closed_in_transaction is False, (
        "the dispatch closed a connection it did not open -- the caller's own, "
        "with a transaction still on it"
    )
    assert Widget.objects.filter(name="no-such-widget").exists() is False


# --- the back half of a call is overridable ----------------------------------
#
# Everything after the dispatch returns -- pagination, rendering, the resolved-
# data pool, the byte bound -- is a seam as argument intake is, so a project
# changing one of them need not replace ``call_tool`` wholesale.


def test_the_run_context_double_carries_every_field_the_package_reads():
    """Pin the stand-in against the real dataclass, in both directions."""
    real = {f.name for f in dataclass_fields(RunContext)}
    assert real >= RUN_CONTEXT_FIELDS_READ, RUN_CONTEXT_FIELDS_READ - real
    double = set(vars(ctx_for("alice")))
    assert double >= RUN_CONTEXT_FIELDS_READ, RUN_CONTEXT_FIELDS_READ - double


@pytest.mark.django_db(transaction=True)
async def test_shape_page_override_replaces_the_slice():
    """The seam is reached for a list selector, with the model's page args."""
    seen = {}

    class Windowed(SpecToolset):
        def shape_page(self, rows, *, ctx, page, limit, max_page_size):
            seen["args"] = (page, limit, max_page_size)
            # A different window, so the default shaper cannot satisfy the assertion.
            return super().shape_page(rows, ctx=ctx, page=1, limit=1, max_page_size=max_page_size)

    user = await sync_to_async(User.objects.create)(username="u")
    await sync_to_async(Widget.objects.create)(name="a", price=1, owner=user)
    await sync_to_async(Widget.objects.create)(name="b", price=2, owner=user)
    toolset = Windowed({"list_widgets": list_spec()})
    tools = await toolset.get_tools(None)

    result = await toolset.call_tool(
        "list_widgets", {"page": 2, "limit": 1}, ctx_for(user), tools["list_widgets"]
    )

    assert seen["args"] == (2, 1, None)
    assert [w["name"] for w in _rows(result)] == ["a"]


@pytest.mark.django_db(transaction=True)
async def test_render_output_override_can_skip_the_projection():
    """The kernel's ``present`` is the opt-out; ``projection=None`` is not."""

    class Unprojected(SpecToolset):
        def render_output(self, spec, result, *, ctx, projection):
            return present(spec, result)

    user = await sync_to_async(User.objects.create)(username="u")
    await sync_to_async(Widget.objects.create)(name="a", price=1, owner=user)
    plain = SpecToolset({"list_widgets": agent_list_spec()})
    unprojected = Unprojected({"list_widgets": agent_list_spec()})
    tools = await plain.get_tools(None)

    projected = _rows(
        await plain.call_tool("list_widgets", {}, ctx_for(user), tools["list_widgets"])
    )
    raw = _rows(
        await unprojected.call_tool("list_widgets", {}, ctx_for(user), tools["list_widgets"])
    )

    assert projected != raw
    assert set(raw[0]) - set(projected[0])


@pytest.mark.django_db(transaction=True)
async def test_render_output_is_handed_the_page_s_rows_and_the_envelope_follows():
    """A paged tool's ``render_output`` presents the page, never the whole set."""
    seen: dict[str, Any] = {}

    class Rows(SpecToolset):
        def render_output(self, spec, result, *, ctx, projection):
            seen["names"] = [row.name for row in result.value]
            return ["rendered"]

    user = await sync_to_async(User.objects.create)(username="u")
    await sync_to_async(Widget.objects.create)(name="a", price=1, owner=user)
    await sync_to_async(Widget.objects.create)(name="b", price=2, owner=user)
    toolset = Rows({"list_widgets": list_spec()})
    tools = await toolset.get_tools(None)

    result = await toolset.call_tool(
        "list_widgets", {"page": 2, "limit": 1}, ctx_for(user), tools["list_widgets"]
    )

    assert seen["names"] == ["b"]
    assert result == {"items": ["rendered"], "page": 2, "totalPages": 2, "hasNext": False}


@pytest.mark.django_db(transaction=True)
async def test_enforce_result_bytes_override_can_bound_on_something_else():
    """Overriding the bound is how a non-byte ceiling gets written at all."""

    class RowCapped(SpecToolset):
        def enforce_result_bytes(self, payload, *, ctx, max_bytes, label):
            if isinstance(payload, dict) and len(payload.get("items", [])) > 1:
                return {"error": f"too many rows for {label}"}
            return super().enforce_result_bytes(payload, ctx=ctx, max_bytes=max_bytes, label=label)

    user = await sync_to_async(User.objects.create)(username="u")
    await sync_to_async(Widget.objects.create)(name="a", price=1, owner=user)
    await sync_to_async(Widget.objects.create)(name="b", price=2, owner=user)
    toolset = RowCapped({"list_widgets": list_spec()})
    tools = await toolset.get_tools(None)

    result = await toolset.call_tool("list_widgets", {}, ctx_for(user), tools["list_widgets"])

    assert result == {"error": "too many rows for list_widgets"}


async def test_tool_call_log_lines_carry_run_correlation_and_usage(caplog):
    """Two concurrent runs interleave; without these fields nothing separates them."""
    spec, _ = _ping_spec()
    toolset = SpecToolset({"ping": spec})
    tools = await toolset.get_tools(None)
    ctx = ctx_for("u", run_id="run-abc", tool_call_id="call-xyz")

    with caplog.at_level(logging.DEBUG, logger="django_spec_agents.spec_tools"):
        await toolset.call_tool("ping", {}, ctx, tools["ping"])

    record = next(r for r in caplog.records if "took" in r.getMessage())
    assert record.run_id == "run-abc"
    assert record.tool_call_id == "call-xyz"
    assert record.run_step == 1
    assert record.run_requests == 0


async def test_permission_denial_log_line_carries_run_correlation(caplog):
    """The one failure with no other trace, so it is the one most worth keying."""

    def peek(user):
        """Peek."""
        return {}

    toolset = SpecToolset({"peek": ServiceSpec(service=peek, permissions=[DenyAll()])})
    tools = await toolset.get_tools(None)

    with (
        caplog.at_level(logging.WARNING, logger="django_spec_agents.spec_tools"),
        pytest.raises(NotPermitted),
    ):
        await toolset.call_tool("peek", {}, ctx_for("u", run_id="run-denied"), tools["peek"])

    record = next(r for r in caplog.records if "Permission denied" in r.getMessage())
    assert record.run_id == "run-denied"


# --- the dispatch carrier and the two remaining log sites ---------------------
#
# ``render_output`` is handed the kernel's whole ``DispatchResult``, so an
# override reads what the default presents nothing of. These cover the carrier
# arriving, the break an override written against the earlier signature takes,
# run correlation on the two remaining log sites, and the derived instructions
# being built once rather than per model step.


def upsert_widget(data, user):
    """Create or update a widget, reporting which of the two happened."""
    widget, created = Widget.objects.update_or_create(
        name=data["name"], owner=user, defaults={"price": data["price"]}
    )
    return SimpleNamespace(widget=widget, created=created)


def widget_from_upsert(result):
    """Re-fetch the upserted row, the way an ``output_selector_spec`` does."""
    return Widget.objects.filter(pk=result.widget.pk)


def upsert_spec(**kwargs):
    kwargs.setdefault("permissions", allow())
    return ServiceSpec(
        service=upsert_widget,
        validator=widget_input(),
        output_selector_spec=SelectorSpec(
            kind=SelectorKind.RETRIEVE,
            selector=widget_from_upsert,
            presenter=widget_presenter(),
        ),
        **kwargs,
    )


@pytest.mark.django_db(transaction=True)
async def test_render_output_reaches_the_dispatch_result_carrier():
    """created-vs-updated is unanswerable from ``value`` once a re-fetch ran."""
    seen: dict[str, Any] = {}

    class Upsert(SpecToolset):
        def render_output(self, spec, result, *, ctx, projection):
            seen["created"] = result.service_result.created
            seen["value_is_the_refetched_row"] = isinstance(result.value, Widget)
            seen["data"] = dict(result.data)
            return super().render_output(spec, result, ctx=ctx, projection=projection)

    user = await sync_to_async(User.objects.create)(username="u")
    toolset = Upsert({"upsert": upsert_spec()})
    tools = await toolset.get_tools(None)

    await toolset.call_tool("upsert", {"name": "a", "price": 1}, ctx_for(user), tools["upsert"])
    assert seen == {
        "created": True,
        "value_is_the_refetched_row": True,
        "data": {"name": "a", "price": 1},
    }

    await toolset.call_tool("upsert", {"name": "a", "price": 2}, ctx_for(user), tools["upsert"])
    assert seen["created"] is False


@pytest.mark.django_db(transaction=True)
async def test_a_render_output_override_written_for_the_earlier_signature_breaks_loudly():
    """An override taking the per-call request, view and extras raises at the call."""
    called: list[str] = []

    class Legacy(SpecToolset):
        def render_output(self, spec, value, *, ctx, projection, many, request, view, extras):
            called.append("yes")
            return value

    user = await sync_to_async(User.objects.create)(username="u")
    await sync_to_async(Widget.objects.create)(name="a", price=1, owner=user)
    toolset = Legacy({"list_widgets": list_spec()})
    tools = await toolset.get_tools(None)

    with pytest.raises(TypeError, match="'many', 'request', 'view', and 'extras'"):
        await toolset.call_tool("list_widgets", {}, ctx_for(user), tools["list_widgets"])

    assert called == []


async def test_dispatch_timeout_log_line_carries_run_correlation(caplog):
    """A tool that intermittently times out is the one worth correlating."""

    toolset = SpecToolset(
        {"crawl": ServiceSpec(service=_crawl, permissions=allow(), atomic=False)},
        dispatch_timeout=0.01,
    )
    tools = await toolset.get_tools(None)

    with (
        caplog.at_level(logging.WARNING, logger="django_spec_agents.spec_tools"),
        pytest.raises(ToolFailed, match="was abandoned"),
    ):
        await toolset.call_tool(
            "crawl", {}, ctx_for("u", run_id="run-slow", tool_call_id="call-slow"), tools["crawl"]
        )

    record = next(r for r in caplog.records if "exceeded its" in r.getMessage())
    assert record.run_id == "run-slow"
    assert record.tool_call_id == "call-slow"


@pytest.mark.django_db(transaction=True)
async def test_result_bound_log_line_carries_run_correlation(caplog):
    """A bound that fires invisibly reads to an operator as a broken tool."""
    user = await sync_to_async(User.objects.create)(username="u")
    await sync_to_async(Widget.objects.create)(name="a", price=1, owner=user)
    toolset = SpecToolset({"list_widgets": list_spec()}, max_result_bytes=1)
    tools = await toolset.get_tools(None)

    with (
        caplog.at_level(logging.WARNING, logger="django_spec_agents.spec_tools"),
        pytest.raises(ToolFailed),
    ):
        await toolset.call_tool(
            "list_widgets", {}, ctx_for(user, run_id="run-fat"), tools["list_widgets"]
        )

    record = next(r for r in caplog.records if "Result bound exceeded" in r.getMessage())
    assert record.run_id == "run-fat"


async def test_derived_instructions_are_built_once_per_toolset(monkeypatch):
    """A pure function of constructor state should not run per model step."""
    calls: list[int] = []
    derive = spec_toolset._derive_instructions

    def counting(*args: Any, **kwargs: Any) -> str:
        calls.append(1)
        return derive(*args, **kwargs)

    monkeypatch.setattr(spec_toolset, "_derive_instructions", counting)
    toolset = SpecToolset({"list_widgets": list_spec()})

    first = await toolset.get_instructions(None)
    second = await toolset.get_instructions(None)

    assert first == second
    assert len(calls) == 1


async def test_an_instructions_override_never_derives_at_all():
    """The memo is lazy, so an override pays nothing for the block it replaces."""
    toolset = SpecToolset({"list_widgets": list_spec()}, instructions="just do it")

    assert await toolset.get_instructions(None) == "just do it"
    assert "_derived_instructions" not in toolset.__dict__


# --- a read-shaping value the render rejects ----------------------------------
#
# A read-shaping param is the one caller input read while the output renders
# rather than while the call dispatches: a ``fields`` selection reaches the
# presenter on the kernel's ``shapes`` and is read as each row is presented. So
# a selection it rejects fails after the dispatch arms have closed, where a raw
# refusal would end the run. These pin what the model gets back, and the cases
# where nothing it sent could have caused the error and the error stays loud.


def _selection(shapes: Any) -> list[str] | None:
    raw = shapes.get("fields")
    return [name.strip() for name in raw.split(",")] if raw else None


class _SelectingPresenter(django_service_specs.Presenter):
    """A row's ``id`` and ``name``, narrowed by a ``fields`` selection it parses."""

    def output(self) -> django_service_specs.Output:
        return django_service_specs.Output(
            (
                django_service_specs.OutputField("id", "integer"),
                django_service_specs.OutputField("name", "string"),
            )
        )

    def present(self, value: Any) -> Any:
        return {"id": value.pk, "name": value.name}

    def presented(self, value: Any, context: Any) -> Any:
        data = self.present(value)
        wanted = _selection(context.shapes) if context is not None else None
        if wanted is None:
            return data
        for name in wanted:
            if name not in data:
                raise ServiceValidationError(f"`{name}` field is not found")
        return {name: data[name] for name in wanted}


class _RejectingPresenter(_SelectingPresenter):
    """A presenter that refuses to render, whatever the call shaped."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def presented(self, value: Any, context: Any) -> Any:
        raise self._exc


def _ordered_widgets(user):
    """The acting user's widgets, in a stable order a page can be asserted on."""
    return Widget.objects.filter(owner=user).order_by("pk")


def _fields_shape(**kwargs: Any) -> django_service_specs.Parameter:
    return django_service_specs.Parameter("fields", "string", nullable=True, **kwargs)


def _paged_spec(
    presenter: django_service_specs.Presenter, *shapes: django_service_specs.Parameter
) -> django_service_specs.SelectorSpec:
    return django_service_specs.SelectorSpec(
        kind=SelectorKind.LIST,
        selector=_ordered_widgets,
        presenter=presenter,
        shapes=Parameters.of(*(shapes or (_fields_shape(),))),
        permissions=[django_service_specs.Unrestricted()],
    )


def _single_spec(presenter: django_service_specs.Presenter) -> django_service_specs.SelectorSpec:
    return django_service_specs.SelectorSpec(
        kind=SelectorKind.RETRIEVE,
        selector=get_widget,
        reads=Parameters.of(Parameter("pk", "integer", required=True)),
        presenter=presenter,
        shapes=Parameters.of(_fields_shape()),
        permissions=[django_service_specs.Unrestricted()],
    )


@pytest.mark.django_db(transaction=True)
async def test_a_selection_written_against_the_envelope_is_one_retry_then_the_page():
    """Through a real run: ``items`` names the page key, and the presenter reads one row."""
    user = await User.objects.acreate(username="alice")
    first = await Widget.objects.acreate(owner=user, name="a")
    second = await Widget.objects.acreate(owner=user, name="b")
    selections = iter(["items", "id, name"])

    def model(messages: list[Any], info: Any) -> ModelResponse:
        if any(is_tool_answer(part) for part in messages[-1].parts):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart("list_widgets", {"fields": next(selections)})])

    toolset = SpecToolset({"list_widgets": _paged_spec(_SelectingPresenter())})
    result = await Agent(FunctionModel(model), deps_type=AgentDeps, toolsets=[toolset]).run(
        "go", deps=AgentDeps(user=user)
    )

    retries = tool_retries(result.all_messages())
    assert [retry.content for retry in retries] == [
        "`fields` was rejected while rendering the result: `items` field is not found. "
        + _SCOPE_DESCRIPTION
    ]
    page = _tool_return(result).content
    assert page["items"] == [{"id": first.pk, "name": "a"}, {"id": second.pk, "name": "b"}]
    assert page["hasNext"] is False


@pytest.mark.django_db
def test_an_unknown_row_field_is_a_retry_not_a_dead_run():
    """Not specific to the envelope: any field the presenter cannot find."""
    user = User.objects.create(username="u")
    Widget.objects.create(owner=user, name="a")
    spec = _paged_spec(_SelectingPresenter())

    with pytest.raises(ModelRetry) as retry:
        _dispatch(spec, user, {"fields": "name, bogus"})

    assert retry.value.message == (
        "`fields` was rejected while rendering the result: `bogus` field is not found. "
        + _SCOPE_DESCRIPTION
    )
    assert _rows(_dispatch(spec, user, {"fields": "name"})) == [{"name": "a"}]


@pytest.mark.django_db
def test_a_tool_that_does_not_page_is_not_told_about_a_page():
    """Retrieve renders one row and has no envelope to misread."""
    user = User.objects.create(username="u")
    widget = Widget.objects.create(owner=user, name="a")

    with pytest.raises(ModelRetry) as retry:
        _dispatch(_single_spec(_SelectingPresenter()), user, {"pk": widget.pk, "fields": "bogus"})

    assert retry.value.message == (
        "`fields` was rejected while rendering the result: `bogus` field is not found."
    )


@pytest.mark.django_db
def test_render_error_with_no_shape_supplied_still_raises():
    """Nothing the model sent shaped this render, so a retry would hide a server bug."""
    user = User.objects.create(username="u")
    Widget.objects.create(owner=user, name="a")
    refusal = ServiceValidationError("`bogus` field is not found")

    with pytest.raises(ServiceValidationError, match="`bogus` field is not found"):
        _dispatch(_paged_spec(_RejectingPresenter(refusal)), user, {})


@pytest.mark.django_db
def test_render_error_from_a_seeded_default_still_raises():
    """A default is seeded when the model omits the argument, so it looks like a sent value."""
    user = User.objects.create(username="u")
    Widget.objects.create(owner=user, name="a")
    spec = _paged_spec(_SelectingPresenter(), _fields_shape(default="name, bogus"))

    with pytest.raises(ServiceValidationError, match="`bogus` field is not found"):
        _dispatch(spec, user, {})


@pytest.mark.django_db
def test_an_explicit_null_selection_renders_every_field():
    """``{"fields": null}`` is a model declining the param, so every field renders."""
    user = User.objects.create(username="u")
    widget = Widget.objects.create(owner=user, name="a")

    result = _dispatch(_paged_spec(_SelectingPresenter()), user, {"fields": None})

    assert _rows(result) == [{"id": widget.pk, "name": "a"}]


@pytest.mark.django_db
def test_render_error_with_an_explicit_null_still_raises():
    """``{"fields": null}`` is how a model says it chose not to fill the param."""
    user = User.objects.create(username="u")
    Widget.objects.create(owner=user, name="a")
    refusal = ServiceValidationError("`bogus` field is not found")

    with pytest.raises(ServiceValidationError, match="`bogus` field is not found"):
        _dispatch(_paged_spec(_RejectingPresenter(refusal)), user, {"fields": None})


@pytest.mark.django_db
def test_a_render_error_that_is_not_a_validation_error_stays_loud():
    """An ``AttributeError`` in a presenter is a server bug whatever was sent."""
    user = User.objects.create(username="u")
    Widget.objects.create(owner=user, name="a")
    spec = _paged_spec(_RejectingPresenter(AttributeError("no such attribute")))

    with pytest.raises(AttributeError, match="no such attribute"):
        _dispatch(spec, user, {"fields": "name"})


@pytest.mark.django_db
@pytest.mark.parametrize(
    "args", [{"fields": "bogus"}, {}], ids=["selection supplied", "nothing supplied"]
)
def test_translate_exception_sees_a_render_error_before_the_default_arm(args):
    """The consumer's map runs first here too; its handler's value is the tool's result as-is."""
    user = User.objects.create(username="u")
    Widget.objects.create(owner=user, name="a")
    refusal = ServiceValidationError("`bogus` field is not found")

    result = _dispatch(
        _paged_spec(_RejectingPresenter(refusal)),
        user,
        args,
        toolset_kwargs={
            "exception_map": {ServiceValidationError: lambda exc: {"translated": exc.detail}}
        },
    )

    assert result == {"translated": "`bogus` field is not found"}


@pytest.mark.django_db
def test_a_render_output_override_is_covered_too():
    """The ``try`` holds the call to the seam, so a custom renderer gets the same containment."""

    class OwnRender(SpecToolset):
        def render_output(self, spec: Any, result: Any, *, ctx: Any, **kwargs: Any) -> Any:
            raise ServiceValidationError("the selection names no field this tool has")

    user = User.objects.create(username="u")
    Widget.objects.create(owner=user, name="a")
    spec = _paged_spec(_SelectingPresenter())
    toolset = OwnRender({"list_widgets": spec})

    with pytest.raises(ModelRetry) as retry:
        toolset._call_spec(spec, user, {"fields": "name"}, ctx=ctx_for(user), label="list_widgets")

    assert retry.value.message == (
        "`fields` was rejected while rendering the result: the selection names no field "
        "this tool has. " + _SCOPE_DESCRIPTION
    )


@pytest.mark.django_db
def test_a_presenter_refusing_a_shape_with_the_kernel_s_refusal_is_a_retry():
    """A presenter refusing a shape raises ``InvalidArguments``: the caller's to fix."""
    user = User.objects.create(username="u")
    Widget.objects.create(owner=user, name="a")
    refusal = InvalidArguments({"fields": ["No field this tool has."]})

    with pytest.raises(ModelRetry) as retry:
        _dispatch(_paged_spec(_RejectingPresenter(refusal)), user, {"fields": "x"})

    assert retry.value.message == (
        "`fields` was rejected while rendering the result: fields: No field this tool has. "
        + _SCOPE_DESCRIPTION
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("args", "subject"),
    [
        ({"query": "{bogus}", "fields": "bogus"}, "`query` or `fields` was"),
        (
            {"query": "{bogus}", "fields": "bogus", "expand": "true"},
            "`query`, `fields` or `expand` was",
        ),
    ],
    ids=["two", "three"],
)
def test_several_supplied_values_are_all_named(args, subject):
    """The presenter does not say which message it refused, so singling one out would guess."""
    user = User.objects.create(username="u")
    Widget.objects.create(owner=user, name="a")
    spec = _paged_spec(
        _RejectingPresenter(ServiceValidationError("`bogus` field is not found")),
        Parameter("query", "string"),
        _fields_shape(),
        Parameter("expand", "string"),
    )

    with pytest.raises(ModelRetry) as retry:
        _dispatch(spec, user, args)

    assert retry.value.message == (
        f"{subject} rejected while rendering the result: `bogus` field is not found. "
        + _SCOPE_DESCRIPTION
    )


@pytest.mark.django_db
def test_a_detail_that_ends_a_sentence_is_not_given_a_second_full_stop():
    """Some refusals carry a full stop and some do not; the retry reads right either way."""
    user = User.objects.create(username="u")
    widget = Widget.objects.create(owner=user, name="a")
    refusal = ServiceValidationError({"name": ["Not selectable."]})

    with pytest.raises(ModelRetry) as retry:
        _dispatch(
            _single_spec(_RejectingPresenter(refusal)), user, {"pk": widget.pk, "fields": "name"}
        )

    assert retry.value.message == (
        "`fields` was rejected while rendering the result: name: Not selectable."
    )


# --- the scope sentence in the instructions -----------------------------------


async def test_the_read_shaping_line_says_what_a_param_applies_to_on_a_page():
    toolset = SpecToolset(shaping({"list": list_spec()}, {"list": [Parameter("query", "string")]}))

    instructions = await toolset.get_instructions(None)

    assert (
        "- Some tools accept read-shaping parameters (`query`) that adjust the shape of the "
        "returned data without filtering it. " + _SCOPE_INSTRUCTION
    ) in instructions


async def test_the_scope_sentence_needs_a_list_tool_that_declares_a_shape():
    """A shape declared only on a retrieve tool never meets a page."""
    toolset = SpecToolset(
        shaping(
            {"list": list_spec(), "get": retrieve_spec()}, {"get": [Parameter("fields", "string")]}
        )
    )

    instructions = await toolset.get_instructions(None)

    assert "read-shaping parameters (`fields`)" in instructions
    assert _SCOPE_INSTRUCTION not in instructions


# --- validation details read as text ------------------------------------------


def _raises(exc: BaseException):
    def service():
        raise exc

    return django_service_specs.ServiceSpec(
        service=service, atomic=False, permissions=[django_service_specs.Unrestricted()]
    )


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        (
            {"name": ["This field is required."], "price": ["A valid integer is required."]},
            "name: This field is required.\nprice: A valid integer is required.",
        ),
        (["First problem.", "Second problem."], "First problem.\nSecond problem."),
        (
            {"address": {"city": ["This field is required."], "zip": ["Too long.", "Digits."]}},
            "address.city: This field is required.\naddress.zip: Too long.\naddress.zip: Digits.",
        ),
        (
            {"non_field_errors": ["The fields name, owner must make a unique set."]},
            "The fields name, owner must make a unique set.",
        ),
        ({"tags": {0: ["Not a valid string."]}}, "tags[0]: Not a valid string."),
        ([{}, {"name": ["This field is required."]}], "[1].name: This field is required."),
        ("Just a sentence.", "Just a sentence."),
    ],
    ids=["dict", "list", "nested dict", "non-field", "positional key", "rows", "string"],
)
def test_a_validation_retry_reads_as_text_not_a_repr(detail, expected):
    """A service's ``ServiceValidationError`` renders one ``path: message`` line per message."""
    with pytest.raises(ModelRetry) as retry:
        _dispatch(_raises(ServiceValidationError(detail)), object(), {})

    assert retry.value.message == expected


@pytest.mark.parametrize("key", ["non_field_errors", "__all__"], ids=["drf-key", "django-key"])
def test_a_message_about_the_whole_input_is_read_without_its_key(key):
    """Form-wide messages arrive under ``__all__`` or ``non_field_errors``."""
    detail = {key: ["Name and owner must make a unique set."], "name": ["Too long."]}

    with pytest.raises(ModelRetry) as retry:
        _dispatch(_raises(ServiceValidationError(detail)), object(), {})

    assert retry.value.message == "Name and owner must make a unique set.\nname: Too long."
