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
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from typing_extensions import NotRequired, TypedDict, Unpack

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
    return _DEFAULTS.pagination.format(page_size=page_size)


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
    """Run one spec through the toolset's dispatch pipeline.

    Calls ``SpecToolset._call_spec`` rather than the module-level function, so
    these tests exercise the same seam ``call_tool`` goes through — including
    the ``call_seeds`` / ``translate_exception`` overrides bound to the run.
    ``reads`` and ``shapes`` are declared on the spec's own, after any it
    declares already: the toolset reads each off the spec.
    """
    toolset_kwargs = toolset_kwargs or {}
    if reads:
        spec = replace(spec, reads=Parameters.of(*spec.reads, *reads))
    if shapes:
        spec = replace(spec, shapes=Parameters.of(*spec.shapes, *shapes))
    # A throwaway toolset per call; construction validation has its own
    # tests, so its warnings are noise here.
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
    """``specs`` as a ``SpecRegistry``, each tool ``reads`` names declaring those
    reads on its spec, after any it declares already.

    A toolset reads a tool's offline contract off its registry entry, so a test
    of a read builds its toolset from this, as a project passing its registry
    does; the schema is the spec's from either source. ``shaping`` in
    ``kernel_specs`` is the same for a spec's shapes.
    """
    registry = SpecRegistry()
    for name, spec in specs.items():
        extra = tuple(reads.get(name, ()))
        registry.register(
            name, replace(spec, reads=Parameters.of(*spec.reads, *extra)) if extra else spec
        )
    return registry


def _rows(result: Any) -> list[Any]:
    """The rows out of a list tool's page envelope.

    Every list-selector result is a page, so the
    tests that are about *what the rows are* unwrap it here rather than each
    re-deriving the wrapper. Asserting the four keys on the way through is what
    keeps this from hiding a regression in the envelope itself: a result that
    stopped being a page would fail here rather than reading as a row that
    happens to lack a ``name``. The envelope's own values have their own tests.
    """
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
    # The block has to describe the shape the toolset actually produces: every
    # terminal arm raises ``ToolFailed``, so an ``{"error": …}`` payload would tell
    # the model to look for a key that is never sent. The wording is asserted here
    # rather than only in the constant.
    assert '{"error"' not in instr
    assert "comes back as a failed call" in instr
    assert "unknown arguments are rejected" in instr
    # A refusal's sentence ends with the rule's code, and the block says so in
    # the shape the call produces: the label as it appears, and the key the same
    # code was advertised under. Both halves, because an instruction naming only
    # the suffix leaves the model to guess what the name refers to.
    assert "`(code: <name>)`" in instr
    assert "`affordances`" in instr
    # A refusal is still final, code or no code.
    assert "that is a final answer, not a reason to retry" in instr


async def test_pagination_line_present_only_with_a_list_selector():
    with_list = await SpecToolset({"list": list_spec()}).get_instructions(None)
    assert _pagination_line(DEFAULT_PAGE_SIZE) in with_list

    # A retrieve selector is a SelectorSpec but not LIST — no pagination line.
    retrieve_only = await SpecToolset({"get": retrieve_spec()}).get_instructions(None)
    assert _pagination_line(DEFAULT_PAGE_SIZE) not in retrieve_only

    # A service spec is not a SelectorSpec at all — no pagination line.
    service_only = await SpecToolset({"go": create_spec()}).get_instructions(None)
    assert _pagination_line(DEFAULT_PAGE_SIZE) not in service_only


@pytest.mark.parametrize("source", ["registry", "mapping"])
async def test_read_shaping_line_lists_declared_query_params_sorted(source):
    """The line names the compiled spec's shapes, so a tool built from a bare
    mapping is told about the same arguments as one built from a registry."""
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
    # "function" is the in-process kind — the run loop invokes call_tool rather
    # than deferring the call to the client.
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
    # ``ordering`` comes from the spec, never from the toolset: this one
    # declares no sort of its own, so none is advertised.
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
    """A declaration of another library's whose ``as_kernel_spec`` refuses it.

    Stands in for a library refusing, where it compiles one, a declaration that
    names no selector to run.
    """

    def as_kernel_spec(self) -> SelectorSpec:
        raise ImproperlyConfigured("SelectorSpec declares no selector")


def test_a_selector_spec_with_no_selector_is_refused_when_the_toolset_is_built():
    """A library refuses a declaration it cannot compile wherever it compiles
    one, and the toolset compiles every spec while it is built. Such a spec
    could never be dispatched, so the mount fails at startup with the library's
    own refusal, prefixed with the tool it is about -- a mapping of many specs
    otherwise leaves the reader to find which one."""
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
    """A sort is the spec's to declare, so this fixture — which declares none —
    is about the slice alone. Ordering *through* pagination is asserted against
    the filter-owned specs further down, where a sort actually exists."""
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
    """An unresolved target is ``ToolFailed``, carrying the wording unchanged.

    The message is what the model reads, and the tool return is marked
    ``outcome="failed"`` -- see ``test_a_business_failure_marks_the_tool_return_failed``,
    which reads that marking off a real run."""
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
    """A list has no single row to be missing, so ``allow_none`` does not turn
    its ``Http404`` into ``None`` (nor into an empty page): it is the same
    ``ToolFailed`` as a read's, whichever way the flag is set."""
    user = User.objects.create(username="u")
    spec = list_spec(selector=_list_raising_http404, allow_none=allow_none)
    with pytest.raises(ToolFailed) as caught:
        _dispatch(spec, user, {})
    assert caught.value.message == "not found"


@pytest.mark.django_db
def test_an_http404_exception_map_entry_is_not_consulted_for_a_selector():
    """The kernel answers a selector's ``Http404`` before it can reach the
    map, so a consumer's entry for it is redundant: the call is the same
    ``ToolFailed`` and the handler never runs."""
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
    """The write never runs: the instance selector is what resolves its target,
    and a missing one is the same ``ToolFailed`` a read gets."""
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
    """Not the presenter's blank row, which would read to the model as a widget
    with an empty name: the kernel presents a single ``None`` as ``None``."""
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
    """A model *is* the thing that can answer, and ``ModelRetry`` is already
    the "here is what to fix, call me again" channel — so no elicitation surface
    or second result type is needed on this transport."""
    with pytest.raises(ModelRetry) as caught:
        _dispatch(ServiceSpec(service=needs_confirmation, atomic=False), object(), {})
    assert "9412 rows match" in str(caught.value)


def test_the_retry_names_the_arguments_to_add() -> None:
    """The keys of ``schema`` are input names, and the model is about to call the
    same tool again — so the names are the actionable part."""
    with pytest.raises(ModelRetry) as caught:
        _dispatch(ServiceSpec(service=needs_confirmation, atomic=False), object(), {})
    assert "`confirmed`" in str(caught.value)


def test_a_bare_message_still_retries() -> None:
    """``schema`` is optional upstream. A message alone is less actionable but
    still better as a retry than as a terminal error."""
    with pytest.raises(ModelRetry) as caught:
        _dispatch(ServiceSpec(service=needs_something_unnamed, atomic=False), object(), {})
    assert str(caught.value) == "This needs something I cannot describe."


def test_it_is_not_swallowed_by_the_generic_service_error_arm() -> None:
    """The ordering trap: ``AdditionalInputRequired``
    subclasses ``ServiceError``, so a handler for the parent catches it first
    unless the specific arm precedes it — which would report a request for input
    as a terminal failure."""
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
    """``require_permissions=False`` keeps its meaning: the spec it warned about
    is called, while a spec that does declare a check is still asked it. The
    grant for the unguarded call is the spec's own and nobody else's, so one
    granting every call is refused by the second assertion."""
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
    """The arguments' shape is checked before the permission check, so a call
    the model got wrong is a retry even where the check would deny it: the
    model is told what it can correct, and learns nothing a schema it was
    already shown does not say."""
    with pytest.raises(ModelRetry, match=r"^name: This field is required\.$"):
        _dispatch(create_spec(permissions=[DenyAll()]), object(), {"price": 1})


@pytest.mark.parametrize("name", ["request", "view"])
def test_a_mount_registering_a_seed_the_call_supplies_is_refused(name):
    """Each call registers ``request`` and ``view`` from the call's context, so
    a mount registering either would have its resolver silently replaced or the
    call refused with a message about registering twice. Said as the
    configuration error it is, naming the registration to remove and where the
    value goes instead."""
    seeds = DEFAULT_POOL_SEEDS.extend(**{name: lambda: None})

    with pytest.raises(
        ImproperlyConfigured,
        match=f"pool_seeds registers '{name}'.*the run's call_context or return it from call_seeds",
    ):
        _dispatch(_ping_spec()[0], object(), {}, toolset_kwargs={"pool_seeds": seeds})


# --- pure helpers ------------------------------------------------------------


def test_shape_list_returns_a_page_rather_than_a_bare_slice():
    """``page`` / ``limit`` are advertised on every list tool, so the shaper
    answers with a total and a clamp, and says whether more rows exist."""
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
    """The clamp is applied on the slice, so the ``totalPages`` reported is
    computed from the limit actually applied."""
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
    """Nothing a model sends is the array it validates, so the list travels under
    one argument and the spec builds. The selector beside it is there because a ``SelectorSpec``
    has no ``many`` to read, so the checks asking which tools take a list have to
    pass over it rather than fail on it."""
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
    """``additionalProperties: false`` survives the merge and stays true: it forbids
    an *undeclared* argument, and a read or a shape is declared."""
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
    # The kernel lists the spec's reads before the list it takes.
    assert schema["required"] == ["project_pk", "items"]
    assert schema["additionalProperties"] is False


@pytest.mark.django_db
def test_declared_channels_beside_the_list_are_not_unexpected_arguments():
    """Both are declared, a read and a shape, which is what keeps the refusal
    of any argument beside the list from reaching them."""
    user = User.objects.create(username="u")

    result = _dispatch(
        bulk_spec(),
        user,
        {"items": [_ITEM], "fields": "name", "project_pk": "7"},
        reads=(Parameter("project_pk", "string"),),
        shapes=(Parameter("fields", "string"),),
    )

    assert [row["name"] for row in result] == ["a"]


@pytest.mark.django_db
def test_a_many_tool_creates_every_item_and_returns_the_rendered_list():
    user = User.objects.create(username="u")

    result = _dispatch(bulk_spec(), user, {"items": [_ITEM, {"name": "b", "price": 2}]})

    assert [(row["name"], row["price"]) for row in result] == [("a", 1), ("b", 2)]
    assert Widget.objects.filter(owner=user).count() == 2


@pytest.mark.django_db(transaction=True)
async def test_a_model_calls_a_many_tool_through_the_run_loop():
    user = await User.objects.acreate(username="u")

    result = await _run_one_tool(
        bulk_spec(), name="create_widgets", user=user, args={"items": [_ITEM]}
    )

    part = _tool_return(result)
    assert part.outcome == "success"
    assert [row["name"] for row in part.content] == ["a"]


@pytest.mark.django_db(transaction=True)
async def test_a_many_tool_advertises_the_array_it_returns():
    """The two halves generated from one spec, compared against each other.

    A ``many=True`` spec renders its list through an ``output_selector_spec``
    declared ``RETRIEVE``, the kind describing one row, so a return schema read
    off that kind would advertise an object while every payload is an array.
    """
    user = await User.objects.acreate(username="u")
    toolset = SpecToolset({"create_widgets": bulk_spec()})

    tools = await toolset.get_tools(None)
    payload = await _call(toolset, "create_widgets", user, {"items": [_ITEM]})
    return_schema = tools["create_widgets"].tool_def.return_schema

    assert return_schema is not None
    assert return_schema["type"] == "array"
    assert isinstance(payload, list)
    assert set(payload[0]) == set(return_schema["items"]["properties"])


@pytest.mark.django_db(transaction=True)
async def test_a_spec_naming_its_list_argument_is_advertised_and_called_by_that_name():
    user = await User.objects.acreate(username="u")
    toolset = SpecToolset({"create_widgets": bulk_spec(many_argument="widgets")})

    tools = await toolset.get_tools(None)
    schema = tools["create_widgets"].tool_def.parameters_json_schema
    payload = await _call(toolset, "create_widgets", user, {"widgets": [_ITEM]})

    assert set(schema["properties"]) == {"widgets"}
    assert schema["required"] == ["widgets"]
    assert [row["name"] for row in payload] == ["a"]


@pytest.mark.django_db
def test_an_invalid_item_is_a_retry_keyed_under_the_argument_by_its_index():
    """Only the invalid item is named, by index: the kernel keys a ``many``
    spec's errors under the list argument by each row's index."""
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
    """What the retry prompt actually carries, read off a real run rather than off
    the exception: one line naming the argument, the item's index and the field,
    then the field's own message -- text, not the repr of an error object. The model corrects the one item and the call goes through."""
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

    retries = [
        part
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, RetryPromptPart)
    ]
    assert len(retries) == 1
    assert retries[0].content == "items[1].price: This field is required."
    assert _tool_return(result).outcome == "success"
    assert await Widget.objects.filter(owner=user).acount() == 2


@pytest.mark.django_db
def test_an_argument_beside_the_list_is_refused_under_reject():
    """The service receives only the list, so the argument has nowhere to go,
    and ``REJECT`` hands it back in the kernel's words before anything runs."""
    user = User.objects.create(username="u")

    with pytest.raises(ModelRetry, match=r"^note: Unknown argument\.$"):
        _dispatch(bulk_spec(), user, {"items": [_ITEM], "note": "x"})

    assert not Widget.objects.exists()


@pytest.mark.django_db
def test_an_argument_beside_the_list_is_dropped_under_ignore():
    """``IGNORE`` means what it means for any undeclared key: the argument is
    dropped without a word and the list is served. ``PASSTHROUGH``, the one
    policy left, is refused when the toolset is built
    (``test_passthrough_is_refused_when_the_toolset_is_built``)."""
    user = User.objects.create(username="u")

    _dispatch(
        bulk_spec(),
        user,
        {"items": [_ITEM], "note": "x"},
        unknown_arguments=UnknownArguments.IGNORE,
    )

    assert Widget.objects.filter(owner=user).count() == 1


def test_a_malformed_schema_fragment_is_refused_naming_the_tool():
    """The toolset reads a spec's ``metadata["json_schema"]`` when it is built,
    and refuses one that is not keyed by phase, in the spec's own terms (``SelectorSpec.metadata[...]``), prefixed with the tool as
    a compile refusal is. Holds the mapping check in ``_schema_fragment``."""
    spec = list_spec(metadata={"json_schema": "not a mapping"})

    with pytest.raises(
        ImproperlyConfigured,
        match=r"^SpecToolset tool 'list_widgets': SelectorSpec\.metadata\['json_schema'\] must be",
    ):
        SpecToolset({"list_widgets": spec})


def test_a_schema_fragment_reaches_the_tool_s_input_schema():
    """A spec's ``metadata["json_schema"]["input"]`` is merged over the schema
    of what it takes, so a description or a keyword the declaration cannot
    spell reaches the model."""
    spec = list_spec(metadata={"json_schema": {"input": {"description": "Widgets."}}})

    assert (
        _input_schema_of(SpecToolset({"list_widgets": spec}), "list_widgets")["description"]
        == "Widgets."
    )


def test_a_schema_fragment_wins_over_the_input_schema_key_by_key():
    """The input fragment is merged over the derived schema one top-level key
    at a time: a key the fragment names replaces the
    derived one, here the ``additionalProperties: false`` the toolset states
    under ``REJECT``, and a key it does not name stays as derived."""
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
            r"SelectorSpec\.metadata\['json_schema'\] declares 'inputs', which name no schema phase\.",
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
    """The rest of the reading: a key naming no phase,
    which is the misplaced fragment the shape invites, and a phase that is not
    a mapping, the ``output`` one included, though nothing reads it on the way
    to the input schema. Holds the unknown-key and the phase-mapping checks in
    ``_schema_fragment``."""
    with pytest.raises(ImproperlyConfigured, match=rf"^SpecToolset tool 'rows': {refusal}"):
        SpecToolset({"rows": list_spec(metadata={"json_schema": declared})})


def test_a_schema_fragment_reaches_the_return_schema_and_wins_key_by_key():
    """The ``output`` phase is merged over the kernel's return schema as the
    ``input`` one is over the parameters, shallowly: a key the fragment names
    is the fragment's whole, and the rest stay derived. Neither phase reaches
    the other's schema."""
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
    """A spec declaring no output has no return schema, and an ``output``
    fragment annotates a derived schema rather than authoring one, so it stays
    ``None``. Holds the ``None`` check before the merge in ``_return_schema``."""
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
    """The shape for a tool that takes arguments beside its list, which ``many=True``
    refuses: the list arrives, and an invalid item is sent back for a retry naming
    its index."""
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
    """A model's tool call is a flat object, and the kernel hands a callable
    only what its spec declares, so an argument outside that set has no channel
    to reach it. The kernel's dispatch raises ``ValueError`` for ``PASSTHROUGH``
    on every call, so the toolset refuses it at build and the mount fails at
    startup.

    The raw value as well as the member, because ``UnknownArguments`` is a
    ``str`` enum and the guard admits the accepted values rather than excluding
    ``PASSTHROUGH``: a string spelling of it must not slip past. ``"drop"``,
    which names no policy at all, is refused the same way, so a misspelt setting
    fails at startup rather than reaching the kernel on the first call.
    """
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
    """The two accepted policies, each on its own, so an allow-list narrowed to
    either one fails here rather than in whichever test happens to use it."""
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
    # A denial aborts the run (NotPermitted) — not a ModelRetry — before the
    # service mutates the row, exactly as it would over HTTP.
    with pytest.raises(NotPermitted):
        _dispatch(spec, other, {"pk": widget.pk, "name": "hacked", "price": 9})
    widget.refresh_from_db()
    assert widget.name == "a"


# --- declaring a shape ---------------------------------------------------------


class _FieldsEcho(Presenter):
    """Reflects a read-shaping ``fields`` back, proving it reached the presenter.

    Stands in for a presenter that branches on what the caller asked of the
    rendering, which the kernel hands it on ``PresentationContext.shapes``.
    """

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
    """Both tools carry the param; only the one returning a page is told that it
    applies to each item rather than to the envelope, after the declared text."""
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
    # A shape is the presenter's, and nothing sorts by it: advertised as the
    # tool's ``ordering``, it would be the sort the model is offered and never
    # applied. Refused from a bare mapping as from a registry, since the check
    # reads the spec.
    registry = shaping(
        {"list_widgets": list_spec()}, {"list_widgets": [Parameter("ordering", "string")]}
    )
    with pytest.raises(
        ImproperlyConfigured, match=r"^SpecToolset tool 'list_widgets': .*\['ordering'\]"
    ):
        SpecToolset(registry if source == "registry" else registry.specs())


@pytest.mark.parametrize("source", ["registry", "mapping"])
def test_a_retrieve_tool_s_shape_may_not_be_named_page(source):
    """A shape is the presenter's, whatever the tool's kind, so ``page`` is
    refused on a retrieve tool too, which takes no pagination of its own: the
    name is reserved for every tool alike."""
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
    """Only a list tool paginates, so on a retrieve tool a read named
    ``page`` or ``limit`` takes nothing from the toolset: it builds from a
    registry, is advertised, and reaches the permission check as sent."""
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
    """A read is what the call carries to the permission check and the
    lookup, not a shape, so ``ordering`` is not the toolset's to take from it:
    the tool advertises it and leaves it in the call, as it leaves a
    selector's own ``ordering``. Holds the shapes-only half of the reserved
    check: checked over every declared read, this tool would be refused.
    """
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
    """A list tool's ``page`` and ``limit`` are its pagination arguments, popped
    before dispatch, so a selector parameter of either name never receives what
    the model sent. Advertised as required, every call would die on a
    ``TypeError`` naming the argument the model had just sent.
    """
    with pytest.raises(ImproperlyConfigured, match=r"tool 'log'.*parameter\(s\) \['page'\]"):
        SpecToolset(
            {
                "log": _name_list_spec(
                    page_of_log, reads=Parameters.of(Parameter("page", "integer", required=True))
                )
            }
        )


def test_a_defaulted_page_parameter_is_refused_too():
    """The shape that would pass silently: each call would run on the selector's defaults,
    ``page=1, limit=10``, whatever page and size the model asked for. Both names
    are listed, in one refusal.
    """
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
    """Only a list tool paginates, so a retrieve selector's ``page`` is the model's
    to send and reaches it. Holds the list-selector condition of the refusal:
    without it this tool would be refused for a name nothing takes away.
    """
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
    """A spec's read of a name its selector takes is the declaration of where
    that parameter comes from. Neither route makes it a separate argument: the
    parameter is advertised once, and its value stays in the call, which hands
    it to the selector and the permission check both, and an omitted one leaves
    the selector its own default.
    """
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
    # And the permission check is handed the value too, on both routes alike.
    assert seen_from_registry == RecordingPermission.seen["arguments"]
    assert seen_from_registry == ({"status": "done"} if args else {})


class RecordingPermission(PermissionCheck):
    """Allows everything, and records what it was shown.

    A kernel ``PermissionCheck``, so a spec takes an instance,
    ``permissions=[RecordingPermission()]``. ``seen`` holds the call's
    ``action`` (the tool's name, off the call's context), the ``view`` seed
    the call carries, its ``arguments`` as the check is handed them, the
    ``user`` it was asked about, and the ``marker`` header of the ``request``
    seed the call carries.
    """

    seen: dict[str, Any] = {}

    def has_permission(self, principal, spec, *, arguments, call_context, request, view):
        RecordingPermission.seen = {
            "action": call_context["action"],
            "view": view,
            "arguments": dict(arguments or {}),
            "user": principal,
            # A header only the request a run arrived on carries, so a call
            # built around some other request reads ``None`` here.
            "marker": request.META.get("HTTP_X_MARKER") if request is not None else None,
        }
        return True


def test_a_read_may_share_a_selector_parameters_name():
    """A read under a selector parameter's name is that parameter's argument,
    the way a spec declares an argument the selector reads, so the refusal of
    a shape under a reserved name leaves it alone."""
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
def test_query_param_reaches_the_serializer_via_request_query_params(args, declared, seen):
    """A shape reaches the presenter on ``PresentationContext.shapes``, under its
    declared default where the model left it out."""
    user = User.objects.create(username="u")
    Widget.objects.create(name="a", price=1, owner=user)
    result = _dispatch(_echo_list_spec(), user, args, shapes=(declared,))
    assert _rows(result) == [{"name": "a", "fields": seen}]


def list_widgets_selecting(user, fields=None):
    """``list_widgets``, taking ``fields`` itself."""
    return Widget.objects.filter(owner=user)


@pytest.mark.django_db
def test_a_read_a_callable_takes_is_its_argument_and_never_the_presenter_s():
    """A read the selector takes by name is the selector's argument, not one of
    the spec's shapes, so the shapes the presenter reads never carry it."""
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
async def test_a_query_param_a_kwargs_selector_takes_is_its_argument_with_its_default():
    """A read a ``**kwargs`` selector takes is the selector's argument rather
    than one of the spec's shapes, so the tool advertises it as a plain
    argument -- not told it applies to each item -- and the default it
    advertises is the one the selector is handed when the model omits it.

    Field selection by the presenter is what such a read gives up: the shapes
    the presenter reads never carry it."""
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
async def test_an_explicit_null_for_a_query_param_a_callable_takes_is_treated_as_omitted(
    spec_kwargs, taken
):
    """``{"fields": null}`` is the model declining to fill the read, as for a
    shape, whichever callable takes it: dropped, so the callable is handed what
    an omitted read is, the read's advertised default, rather than the call
    being refused as a null the parameter does not admit."""
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
    """The read's declaration stands for the parameter of its name, so the tool
    advertises the read's default, and a call leaving the read out has to be
    handed that default rather than the parameter's own."""
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
    """The default stands in for a value left out, never for one sent: a call
    sending ``fields`` hands the selector the model's value. Holds the
    ``name not in args`` in ``_fill_reads``."""
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
def test_an_explicit_null_query_param_is_treated_as_omitted(declared, seen):
    """``{"fields": null}`` is a model declining to fill the param. It must not
    reach the presenter as the string ``"None"``, and a default still applies."""
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
    # A closed-input list selector under REJECT: an undeclared arg would raise
    # ModelRetry. A declared shape is part of the argument set, so this passes.
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
    # A plain Agent adding SpecToolset to ``toolsets=`` (no capability) gets the
    # conventions: Pydantic-AI collects the toolset's ``get_instructions``.
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
    """The one tool return in a finished run's messages."""
    returns = [
        part
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
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
    """A ``ServiceError`` reaches the wire as a failed call, never as a
    *successful* one whose payload happens to read like a refusal."""
    result = await _run_one_tool(ServiceSpec(service=boom, permissions=allow(), atomic=False))
    part = _tool_return(result)
    assert part.outcome == "failed"
    # The message the model reads is unchanged by the marking.
    assert part.content == "nope"
    # And the run survived it, which is what separates ``ToolFailed`` from an
    # ordinary exception: the model got to answer.
    assert result.output == "done"


async def test_a_refused_call_marks_the_tool_return_failed_and_carries_its_code():
    """What reaches the wire for an ``ActionUnavailable``: the same ``failed``
    marking as any business error, and content that is the reason followed by the
    code. ``content`` is what a transport forwards as the tool result, so this is
    the text a browser rendering the call shows as well as what the model reads.

    The books close *while the model is answering*: the step that offered
    ``close_books`` asked its condition and found it met, and the call that
    followed is refused. That is the one way a run reaches this refusal through
    a listing, since an unmet condition leaves the tool out of the catalog."""
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
    """The dispatch succeeded and the *result* was refused. ``failed`` is still
    the truthful marking: nothing usable reached the model."""
    user = await sync_to_async(User.objects.create)(username="alice")
    await sync_to_async(Widget.objects.create)(name="a", price=1, owner=user)

    result = await _run_one_tool(list_spec(), name="list_widgets", user=user, max_result_bytes=20)
    part = _tool_return(result)
    assert part.outcome == "failed"
    assert "byte ceiling" in str(part.content)


async def test_an_exception_map_handler_that_returns_a_value_still_succeeds():
    """``exception_map`` is documented as returning *the tool's result*, and a
    returned value is what pydantic-ai marks ``"success"``. A project using the
    seam to turn an exception into an ordinary answer -- a "nothing matched" it
    would rather express as an empty payload than as a refusal -- keeps getting
    a successful call, not a failed one wearing its payload."""
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
    # A filter_set selector's fields land in the tool schema via
    # the kernel's spec_input_schema, with no read declared for them.
    toolset = SpecToolset({"list_widgets": _filtered_list_spec()})
    tools = await toolset.get_tools(None)
    assert "min_price" in tools["list_widgets"].tool_def.parameters_json_schema["properties"]


@pytest.mark.django_db
def test_filter_set_filters_via_ordinary_params_not_query_params():
    user = User.objects.create(username="u")
    Widget.objects.create(name="cheap", price=1, owner=user)
    Widget.objects.create(name="pricey", price=10, owner=user)
    # The filter value is an ordinary tool arg: a filter_set selector's declared
    # set is open (REJECT doesn't flag it), and dispatch hands it to the FilterSet
    # as filter_data. No read is declared for it.
    result = _dispatch(_filtered_list_spec(), user, {"min_price": "5"})
    assert [w["name"] for w in _rows(result)] == ["pricey"]


@pytest.mark.django_db
def test_reject_refuses_a_key_outside_a_filter_sets_surface():
    """A ``filter_set``'s filters are declared names like a parameter is, so the
    set they make is closed: a key outside the filters and the selector's own
    parameters is refused under ``REJECT`` like any invented key. Holds the
    ``unknown_arguments`` docstring and the quickstart to calling that set
    closed."""
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
    """Under ``IGNORE`` a key no parameter declares is dropped, and a bare
    ``**kwargs`` declares none, so the callable is not handed it."""
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
    """The kernel closes the argument set at the spec's declared parameters, and a
    bare ``**kwargs`` declares none, so under ``REJECT`` a key sent to it is
    refused like any other undeclared one."""
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
    """Scoping provider: derive a price ceiling from the call's ``project_pk``.

    Stands in for a consumer's scoping provider that reads a route capture. It
    takes the value by name, as an argument: off HTTP there is no view whose
    ``kwargs`` carry it.
    """
    return {"ceiling": int(project_pk) if project_pk is not None else 0}


def list_under_ceiling(user, ceiling):
    """List the user's widgets priced at or below the project's ceiling."""
    return Widget.objects.filter(owner=user, price__lte=ceiling)


def _provider_scoped_spec():
    # ``project_pk`` is consumed by the provider, by name -- the selector never
    # declares it, so it is a pure provider-read (not a selector argument).
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
    # The selector takes no ``project_pk``; under REJECT an argument nothing
    # declares would raise. The read is what declares it, so the provider reads
    # it and REJECT does not.
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
    """A list over ``list_in_project``, whose ``project_pk`` is the selector's own.

    The read is marked ``reflected``, as a library compiling a spec marks a
    read it takes off the selector's signature (here its ``Unpack`` extras): a
    test replacing ``reads`` declares the same name itself.
    """
    return SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_in_project,
        reads=Parameters.of(Parameter("project_pk", "integer", reflected=True)),
        presenter=widget_presenter(),
        permissions=allow(),
    )


@pytest.mark.django_db
def test_a_reflected_extras_key_reaches_a_provider_naming_it_too():
    """A provider reading a name by argument needs no read of its own to be handed it.

    A key reflected from the selector's ``Unpack`` extras is delivered as an
    argument, and a scoping ``spec.kwargs`` provider taking the same name is
    handed that argument as well: the value is read by name.
    """
    seen = {}

    def scope_provider(project_pk=None):
        seen["project_pk"] = project_pk
        return {}

    user = User.objects.create(username="u")
    Widget.objects.create(name="cheap", price=5, owner=user)
    spec = replace(_dual_declared_spec(), kwargs=scope_provider)
    # Supplied as an ordinary argument, with no read declared beside the reflected one.
    result = _dispatch(spec, user, {"project_pk": 10})
    assert [w["name"] for w in _rows(result)] == ["cheap"]  # the selector did get it
    assert seen == {"project_pk": 10}  # and so did the provider


@pytest.mark.django_db
def test_a_declared_read_reaches_the_selector_s_extras():
    user = User.objects.create(username="u")
    Widget.objects.create(name="cheap", price=5, owner=user)
    Widget.objects.create(name="dear", price=15, owner=user)
    # Declared on the spec, the read is an argument typed by its own
    # declaration, which dispatch hands to the selector, where the Unpack
    # extras read it.
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
    """The schema is the spec's, so a tool built from a mapping requires the
    read its spec declares required, as one built from the registry does."""
    spec = replace(
        list_spec(), reads=Parameters.of(Parameter("project_pk", "integer", required=True))
    )

    schema = _input_schema_of(SpecToolset({"list_widgets": spec}), "list_widgets")

    assert schema["required"] == ["project_pk"]


def test_omitting_a_required_read_raises_model_retry():
    # Schema ``required`` is a hint models routinely ignore, so the runtime has
    # to give the model a way back: ModelRetry naming the argument, not a crash.
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
    """The kernel checks the lookup's argument and the input together, so a
    service call sending a mistyped lookup value and leaving out a field is
    handed one retry naming both, a line each, and the model fixes the call in
    one turn rather than two."""
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
    """The kernel's reading of a required read that declares a default: an
    absent value is not refused, the default is it. The toolset builds such a
    tool and fills the default into an omitted call, rather than refusing a
    declaration the spec compiled.
    """
    read = Parameter("project_pk", "string", default="P1", required=True)
    spec = list_spec(permissions=[RecordingPermission()])
    # A registry builds it as a mapping does.
    SpecToolset(reading({"list_widgets": spec}, {"list_widgets": [read]}))

    _dispatch(spec, None, {}, reads=[read])

    assert RecordingPermission.seen["arguments"] == {"project_pk": "P1"}


# Wording for refusals of a mount's channel declarations that the toolset does not
# make: a spec cannot make any of these declarations unrefused, so the toolset
# carries no check for them. Each case below is refused one layer down, and must
# not be answered in these words.
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
    """The kernel refuses each of these declarations when the spec is built,
    and a required read withheld by ``tool_server_arguments`` when the toolset
    binds it, so a toolset never serves one: the reason the toolset carries no
    check of its own for them."""
    with pytest.raises(ImproperlyConfigured, match=refusal) as raised:
        build()

    assert not any(wording in str(raised.value) for wording in _REFUSALS_THE_TOOLSET_DOES_NOT_MAKE)


# --- the host absolute URLs are made against ---------------------------------


class _Fileish(Presenter):
    """Renders a file's URL absolute against the call's ``request``, relative without one.

    The branch a file field's rendering takes: the request is read off the
    call's context, where the toolset puts the one the run arrived on.
    """

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
    """The host a URL is made absolute against is the request the run arrived on.

    Put in the run's ``call_context`` under ``request``, it is the request a
    presenter reads off the call's context; a run that arrived on none renders
    a relative URL, as a presenter handed no request does.
    """
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
    """Over HTTP ``None`` means *inherit*; off HTTP there is nothing to inherit.

    A spec guarded behind a viewset, with passing HTTP tests, is callable by
    whatever the model decides to call the moment it reaches a toolset. There is
    no signal anywhere, which is why this has to fire at construction.
    """
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
    """``permissions=[Unrestricted()]`` is a declaration that anyone may call;
    only a spec declaring no permissions at all is the absence the toolset
    refuses. The kernel's own ``unguarded_specs`` would flag this spec, which
    is why the toolset asks the spec's ``permissions`` rather than it."""
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
    """The tool schema is advisory and the args validator is a no-op, so a value
    of any shape reaches the pop untyped. Nothing here inspects it: the tool
    advertises no sort, so what it *is* cannot matter."""
    with pytest.raises(ModelRetry, match="does not accept an `ordering` argument"):
        _dispatch(list_spec(), object(), {"ordering": ordering})


async def test_the_ordering_instruction_is_absent_when_no_tool_advertises_a_sort():
    """Advice a model cannot act on is not free — it is prompt budget spent
    teaching it about an argument that will be rejected."""
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
    """Public sort names that are *not* the ORM paths they resolve to.

    ``cost`` / ``title`` map to ``price`` / ``name`` through the filter's own
    ``param_map``, which is why raw ORM paths are not interchangeable with these
    and why the filter has to be the one applying them.
    """

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
    """The same sort, declared under a name that is not ``ordering``.

    Nothing requires the name. The docs suggest ``ordering``, and a project
    following that to the letter is fine, but the name is the project's to
    choose and django-filter has no opinion about it.
    """

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
    """The name is the project's to choose, and nothing requires ``ordering``.

    A check of the literal string would read a FilterSet declaring ``sorting``
    as "owns no ordering" -- dropping the usage instruction and, more quietly,
    leaving the value in the callable's kwarg pool.
    """
    assert _spec_ordering_argument(_sorting_list_spec()) == "sorting"


def test_a_plain_filter_is_not_mistaken_for_a_sort():
    # min_price is a NumberFilter. Only OrderingFilter defines
    # ``get_ordering_value``, which is what makes the duck-type specific.
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
    """Popped, not merely applied.

    The FilterSet reads ``params`` either way, so the rows would come back sorted
    even with the value left in the selector's kwarg pool, where a selector
    declaring ``**kwargs`` would receive a read-shaping argument it never asked
    for: the hazard the expected failure's reason names, for a renamed sort.
    """
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
    """A hard-coded name in the prose is a second copy of the same assumption.

    An instruction naming an argument the schema does not carry is worse than no
    instruction: the model is told to send something that will be rejected.
    """
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
    """The FilterSet advertises its sort, so the value has to reach it and the
    rows have to come back sorted. Asserting the order rather than "no error" is
    the point — the failure being guarded against is a tool that answers
    cheerfully with rows in the wrong order.
    """
    user = User.objects.create(username="u")
    _three_widgets(user)
    result = _dispatch(build(), user, args)
    assert [w["name"] for w in _rows(result)] == names


@pytest.mark.django_db
def test_filter_owned_ordering_composes_with_the_other_filters():
    """``filter_data`` *replaces* ``params`` as the filter source rather than
    adding to it, so routing ``ordering`` through it has to carry the rest of the
    filter args along — otherwise ordering a filtered list silently unfilters it.
    """
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
    """The FilterSet validates it — including against the raw ORM path behind one
    of its own public choices, which is not itself a choice. The kernel raises
    that as invalid arguments, which is already the ``ModelRetry`` arm;
    the toolset adds no second validation of its own.
    """
    user = User.objects.create(username="u")
    _three_widgets(user)
    with pytest.raises(ModelRetry, match="ordering"):
        _dispatch(_ordered_list_spec(), user, {"ordering": "price"})


async def test_the_advertised_ordering_enum_is_the_filters_own_vocabulary():
    """The kernel's schema reads an ``OrderingFilter`` as the filter's widget
    reads it: an array of sort names, each one of the filter's public choices.
    The vocabulary reaching the model is the filter's public one, and it is the
    only one there is.
    """
    toolset = SpecToolset({"list_widgets": _ordered_list_spec()})
    tools = await toolset.get_tools(None)
    props = tools["list_widgets"].tool_def.parameters_json_schema["properties"]
    assert props["ordering"]["type"] == "array"
    assert props["ordering"]["items"] == {
        "type": "string",
        "enum": ["cost", "-cost", "title", "-title"],
    }
    # The kernel's bound on how many sort keys one call may send.
    assert props["ordering"]["maxItems"] == 1000


@pytest.mark.django_db
def test_a_sort_sent_as_one_string_is_handed_back_as_a_retry():
    """The schema advertises the sort as an array, and the kernel's shape check
    refuses anything else, so a string is the model's mistake to correct rather
    than a value read for it: the toolset no longer splits one on commas."""
    user = User.objects.create(username="u")
    _three_widgets(user)

    with pytest.raises(ModelRetry, match=r"^ordering: Expected an array\.$"):
        _dispatch(_ordered_list_spec(), user, {"ordering": "cost"})


def test_spec_owns_ordering_reads_the_reflected_schema():
    assert _spec_ordering_argument(_ordered_list_spec()) == "ordering"
    assert _spec_ordering_argument(_filtered_list_spec()) is None
    assert _spec_ordering_argument(list_spec()) is None
    # Only a list selector can contest the argument: nothing else is ever offered
    # one, so a service with an `ordering` input field is not a clash.
    assert _spec_ordering_argument(retrieve_spec()) is None
    assert _spec_ordering_argument(create_spec()) is None


def test_the_filters_vocabulary_survives_the_schema_merge_untouched():
    """``_input_schema`` adds the transport's ``page`` and ``limit`` beside the
    spec's own properties, so anything it wrote under a name the filter already
    advertised would silently replace the filter's public choices. It writes
    those two and nothing else, and this is the assertion that says so at the
    property that would notice.
    """
    props = _input_schema(_ordered_list_spec())["properties"]
    assert props["ordering"]["items"]["enum"] == ["cost", "-cost", "title", "-title"]
    assert {"page", "limit"} <= set(props)


async def test_the_ordering_instruction_is_emitted_for_a_filter_owned_tool():
    """The tool whose sort is a set of labelled options — the one that most needs
    to be told exactly one of them is picked, not a comma-separated list."""
    instructions = await SpecToolset({"list_widgets": _ordered_list_spec()}).get_instructions(None)
    assert "accept `ordering`" in instructions


@pytest.mark.django_db
def test_filter_owned_ordering_survives_pagination():
    """Sort and slice are owned by different layers — the FilterSet orders the
    lazy queryset, ``paginate_output`` slices it afterwards — so the pair has to
    be asserted together. Sorted by ``cost`` the rows are b, c, a; the second
    page of two is therefore the *largest*, which no unsorted result produces
    reliably.
    """
    user = User.objects.create(username="u")
    _three_widgets(user)
    result = _dispatch(_ordered_list_spec(), user, {"ordering": ["cost"], "page": 2, "limit": 2})
    assert [w["name"] for w in _rows(result)] == ["a"]
    assert (result["page"], result["totalPages"], result["hasNext"]) == (2, 2, False)


class _BogusTargetFilterSet(django_filters.FilterSet):
    """A sort whose public choice maps to something that is not a column.

    An author's error that cannot be caught at construction: knowing whether a
    ``param_map`` target is real means asking a queryset that does not exist yet.
    """

    ordering = django_filters.OrderingFilter(fields=(("nope", "broken"),))

    class Meta:
        model = Widget
        fields = []


@pytest.mark.django_db
def test_a_sort_target_that_is_not_a_column_raises_rather_than_retrying():
    """Pinned because it is easy to assume the opposite.

    Django validates a plain-string ``order_by`` eagerly, so the FilterSet raises
    inside the kernel's ``dispatch`` and no arm turns the ``FieldError`` into a
    ``ModelRetry`` -- which is arguably right, since the model cannot correct a
    mis-declared ``param_map`` by choosing a different sort.
    """
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
    """``page`` / ``limit`` / ``ordering`` are the transport's, whoever consumes them.

    A selector declaring ``**kwargs`` receives the whole dispatch pool, so this is
    the test that catches a transport arg being left in ``params`` — including the
    filter-owned ``ordering``, which reaches the FilterSet through ``filter_data``
    precisely so that it does *not* reach the callable.
    """
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
    # The selector's own filter arg is untouched — only the transport's are taken.
    assert seen["min_price"] == "1"

    # The FilterSet gets the filter args plus ordering, and *not* the pagination
    # args: ``filter_data`` is built after ``page`` / ``limit`` have been popped,
    # so routing ``ordering`` there does not widen what the FilterSet sees.
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
    """The schema drops a name the toolset fills, and the call agrees with it: a
    sort the model sends anyway is refused like any other the tool does not
    advertise, instead of being passed on to a selector that is handed the
    deployment's sort whatever the model asked for.

    ``_spec_ordering_argument`` reads the reflection with the names the toolset
    fills, so it agrees with the schema that ``ordering`` is gone. The
    pool-seed case holds ``pool_seeds`` reaching it from ``_pop_pagination``.
    """
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
    """No line teaching the model a sort argument no tool lets it send, in the
    block for a step offering every tool and in the one for a step leaving a tool
    out, which are derived separately. The pool-seed case holds ``pool_seeds``
    reaching ``_spec_ordering_argument`` from each."""
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
    """The caller supplies the sink; the toolset only carries it.

    A toolset that *constructed* one — "write progress to the logger" — would
    have picked a transport it does not own, which is the thing this package
    exists not to do.
    """
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
    """The one failure with no other trace.

    A retry reaches the model and a failed call reaches its answer, but a
    denial aborts the run and is absorbed by whatever is driving it — so over
    HTTP this is a `403` in the access log and here it would be nothing at all.
    """
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
    """The kernel's ``NotPermitted``, raised bare by the service itself rather
    than by a permission check, is answered by the denial arm all the same."""

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
async def test_a_deactivated_principal_is_logged_as_a_denial(caplog):
    """The kernel refuses a deactivated principal before any permission check,
    with ``PrincipalUnavailable`` rather than ``NotPermitted``; it is still a denial,
    and still the one failure with no other trace."""
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
    """A partial payload looks complete.

    A list cut at the byte ceiling is indistinguishable from a list that had
    that many rows, so a model would answer confidently from data it does not
    know is missing. Refusing costs a turn; truncating costs correctness.
    """
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
    """The unbounded read is the one that hurts, and it is the one a model
    produces by simply not thinking about pagination."""
    user = User.objects.create(username="u")
    for n in range(5):
        Widget.objects.create(name=f"w{n}", price=n, owner=user)

    assert len(_rows(_dispatch(list_spec(), user, {}, max_page_size=3))) == 3
    # With no ceiling configured, an omitted limit is the default page size --
    # not "everything", which would make an unremarked truncation possible.
    # Five rows fit inside one default page, so
    # they all come back, and ``hasNext`` says there is no more.
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
    """The motivating case: Django's ``ValidationError``, not the kernel's
    ``ServiceValidationError``.

    Raised by ``full_clean`` and any custom model validator, absent from the
    translated set, and so fatal to the run where the kernel's twin would have
    been a retry. Broadening the built-in set fixes that one; a map fixes the class.
    """
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
    """Deliberate: the built-ins are this package's guess, not a law.

    A ``ServiceError`` normally fails the call. A project that would
    rather the model retry must be able to say so, or the map is only half a
    seam.
    """
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
    """There is no view off HTTP: a ``kwargs=`` provider written for one, which
    declares ``view``, is handed the call's ``view`` entry, ``None`` where the
    run put none, rather than a stand-in whose ``kwargs`` would carry captures
    by a second route."""
    seen: dict[str, Any] = {}

    def scope(view):
        seen["view"] = view
        return {}

    _dispatch(list_spec(kwargs=scope), User.objects.create(username="u"))

    assert seen == {"view": None}


@pytest.mark.django_db
def test_a_request_call_seeds_returns_stands_over_the_one_the_run_put():
    """A subclass reaches dispatch with the run's typed deps in hand.

    The tenant is per-run, so it cannot be baked into the toolset at
    construction: the override reads it off ``ctx.deps``, and the request it
    returns is the call's ``request`` over the one the run arrived on.
    """
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
    """``get_tools`` is async and needs a ``RunContext``, so it cannot answer here.

    A caller composing this toolset into a name-dedup pass or a tool catalog does
    so at configuration time, with no run in sight. Without a public answer they
    reach for ``_specs``, and a private becomes load-bearing across a package
    boundary.
    """
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
    """A widget marked for an agent: its handle, its label, a hidden price, a spoken status.

    Every widget is in stock here, so ``status`` is the one value it renders.
    """

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


# ``transaction=True`` because neither write below can be rolled back. ``acreate``
# reaches the ORM through ``sync_to_async``, which in an async test with no
# synchronous frame above the loop lands on asgiref's shared thread -- a different
# thread, so a different connection, in autocommit -- while pytest-django's
# rollback wraps this thread's connection and nothing else. Both rows would commit
# and outlive the test, to be read by whichever later test counts rows, which then
# fails for a reason nowhere near itself.
#
# Creating them on the test's own thread instead is the cheaper-looking fix and it
# does not work: the dispatch under test runs on that same shared thread, and from
# there rows still uncommitted in this thread's transaction are not visible at all
# -- measured, not assumed. The assertion is about exactly those rows, so it would
# read back an empty page.
#
# The cost is a flush of every table at teardown rather than a rollback. The two
# other tests in this file that write from a dispatch thread pay it for the same
# reason.
@pytest.mark.django_db(transaction=True)
async def test_call_tool_uses_the_projection_built_at_construction():
    user = await User.objects.acreate(username="u2")
    widget = await Widget.objects.acreate(name="Sprocket", price=100, owner=user)
    toolset = SpecToolset({"widgets": agent_list_spec()})

    rows = await toolset.call_tool("widgets", {}, ctx_for(user), None)

    # ``widget.pk`` rather than a literal 1: a rolled-back test always starts from
    # an empty table, but a truncating one only restarts the ids on backends that
    # reuse them, and what this asserts is the projection, not the numbering.
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
def test_a_query_param_declaring_no_default_contributes_nothing(no_default: Any) -> None:
    """Both sentinels for "no default", ``None`` and ``UNSET``, read as absent,
    for a shape and an advertised read alike.

    ``UNSET`` is the "no default" that lets ``default=None`` mean an explicit
    null, and a check of only ``is not None`` reads it as a real value and hands
    the sentinel object to the spec as an argument.
    """
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
    """A read marked ``reflected``, which a library compiling a spec takes off a
    callable's signature, carries the callable's own default, which the
    callable applies; only a declared read's default is the toolset's to fill."""
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
async def test_the_tool_name_is_the_view_action_a_permission_class_reads():
    """The call's ``action`` is the tool name, never ``None``.

    A permission check branching on the action -- strict for the actions it
    knows, permissive otherwise -- would take the permissive arm on every call
    given ``None``, so the same spec behind the same class would be gated one way
    over HTTP and another way through an agent. The tool name is the identity the model
    called, and it is what the MCP transport reports for the same spec.
    """
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
    """The tool's name is the call's ``action``, and a view is how to say another.

    A project whose permission check branches on viewset action names returns
    a view carrying one of those from ``call_seeds``, which the check reads as
    its ``view`` seed, where the run put none.
    """

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
    """Reads its call's ``fields`` shape either side of a pause.

    The pause is what lets a second call, run concurrently, rewrite a context
    the two shared before the first reads it again.
    """

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
    """The request a run arrived on is one object for the whole run, and every
    call of the run is built around it, concurrently where the model asks for
    several at once. Each call's presenter reads the call's own ``fields``
    throughout its render, never the other call's, and the arrived-on request
    keeps its own query string."""
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
    """The second half: the request the run arrived on survives the call unchanged.

    A project serving that request must be able to keep reading it afterwards
    -- a later permission check, an audit hook, a ``request.method`` branch.
    A dispatch that forced ``method`` to ``POST``, replaced ``GET`` or
    reassigned ``user`` on that very object would have effects that outlived
    the call.
    """
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

    # The call was built around this request, so what follows is about a
    # request the dispatch had in hand, not one it never saw.
    assert RecordingPermission.seen["marker"] == "arrived"
    assert ambient.method == "GET"
    assert ambient.GET.dict() == {"fields": "original"}
    assert ambient.user == "the-operator"


# --- the tool catalog --------------------------------------------------------


async def test_every_tool_is_listed_by_default():
    """The documented posture: listing discloses, ``permission_classes`` gate.

    A permission whose answer depends on the arguments has none to read at
    listing time, so filtering by default would hide tools the caller can
    actually invoke.
    """
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
    """Hiding is a disclosure decision, never an authorization one.

    Nothing routes tool calls through the catalog's absence, so an override that
    hides the wrong tool must not become the only thing standing between a model
    and a spec.
    """

    class HideEverything(SpecToolset):
        async def is_tool_listed(self, name, ctx):
            return False

    toolset = HideEverything({"denied": create_spec(permissions=[DenyAll()])})

    assert await toolset.get_tools(ctx_for(object())) == {}
    # Well-formed arguments, because the shape check answers before the
    # permission check does: the call has to reach the gate to show it holds.
    with pytest.raises(NotPermitted):
        await toolset.call_tool("denied", {"name": "x", "price": 1}, ctx_for(object()), None)


# --- operations unavailable right now ----------------------------------------
#
# A ``ServiceSpec`` affordance answered without a row -- a callable ``when`` --
# is asked each step, and a tool it refuses is left out of the catalog and named,
# with its reason, in the instructions. Unlike a permission, it reads only seeds,
# so it cannot hide a tool the caller could have invoked with other arguments.


def _approve() -> dict[str, bool]:
    """Approve the pending invoices.

    Takes no arguments, so a ``fields`` the spec declares is one of its shapes
    rather than this service's argument.
    """
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
    """There is no row at listing time, so a condition on one is never asked here.

    Unmet on every row -- ``pk__in=[]`` matches nothing -- and listed anyway,
    because it is answered per object at the call. A hop or a pool here would be
    the cost of asking a question with no answer; stubbing both to fail proves
    neither happens, and with no hop no query can have run either: the ORM is
    closed to the event loop.
    """

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
    monkeypatch.setattr(spec_toolset, "base_pool", no_pool)

    tools = await toolset.get_tools(ctx_for(User(username="u")))

    assert sorted(tools) == ["cancel", "orders"]


async def test_a_toolset_declaring_no_condition_builds_no_pool_and_reads_no_user(monkeypatch):
    """The ordinary toolset pays nothing: no pool, no hop, not even the user.

    ``get_user`` raising is the sharpest probe of "nothing is built": the user
    is the first thing a pool needs, and the extractor is project code that may
    be expensive.
    """
    built: list[Any] = []
    monkeypatch.setattr(spec_toolset, "base_pool", lambda **kw: built.append(kw))

    def no_user(ctx: Any) -> Any:
        raise AssertionError("a toolset with no condition read the user to list")

    toolset = SpecToolset({"list_widgets": list_spec(), "make": create_spec()}, get_user=no_user)

    tools = await toolset.get_tools(ctx_for())
    instructions = await toolset.get_instructions(ctx_for())

    assert sorted(tools) == ["list_widgets", "make"]
    assert instructions == toolset._derived_instructions
    assert built == []


async def test_is_tool_listed_returning_true_does_not_relist_an_unavailable_tool():
    """The omission is not inside the overridable default, so an override that
    lists everything -- and never calls ``super()`` -- cannot switch it off."""

    class ListEverything(SpecToolset):
        async def is_tool_listed(self, name, ctx):
            return True

    toolset = ListEverything(
        {"list_widgets": list_spec(), "approve": _conditioned_spec(lambda: False)}
    )

    assert sorted(await toolset.get_tools(ctx_for(User(username="u")))) == ["list_widgets"]


async def test_the_instructions_name_an_unavailable_operation_and_describe_only_the_rest():
    """Everything describing the omitted tool goes, and its name and reason arrive.

    ``approve`` is the only tool declaring the ``fields`` read-shaping param, so
    the line naming ``fields`` is present exactly when ``approve`` is offered --
    which is what shows the block is derived from the tools this step offers
    rather than from all of them. ``ping`` has a condition that holds, and is
    named nowhere: the instructions list what is missing, never what is there.
    """
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
    """A shared instance, interleaved runs, conditions answering differently.

    A toolset is typically a module-level singleton serving every run in the
    process, so nothing it remembers may carry one caller's availability into
    another's catalog or instructions. Same ``run_id`` and ``run_step`` on
    both contexts on purpose: a memo keyed on those would be the leak.
    """
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
    """A step pays the derivation only for a combination the toolset has not seen.

    Two different users both missing ``approve`` share one derivation, because
    the block is a function of which tools are offered and nothing else -- the
    reason lines, which are, are rendered per step outside the memo.
    """
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
    """Django raises ``SynchronousOnlyOperation`` for ORM access on the loop.

    A condition is user code and ``Period.current().is_open`` is the ordinary
    shape of one, so listing has to hop exactly as a dispatch does.
    """
    toolset = SpecToolset({"approve": _conditioned_spec(lambda: Widget.objects.exists())})

    tools = await toolset.get_tools(ctx_for(User(username="u")))

    assert tools == {}


@pytest.mark.parametrize(
    "arrived",
    [None, RequestFactory().get("/", {"scope": "all"})],
    ids=["no request", "the request the run arrived on"],
)
async def test_a_condition_sees_the_request_and_user_its_call_would(arrived):
    """Asked at listing time, then again by the call's own enforcement.

    The two must see the same object under ``request`` -- the one the run
    arrived on, or ``None`` for a run that arrived on none -- or a condition
    reading it answers one way when the catalog is built and another when the
    call is refused, and the listing stops predicting the call.
    """
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
    """The listing asks a condition with the seeds the call would register.

    A project registering per-run state -- a tenant read off ``ctx.deps`` -- has
    conditions that read it. Asked any other way, the listing would answer them
    against seeds the call never uses.
    """

    class Tenanted(SpecToolset):
        def call_seeds(self, *, ctx, tool_name):
            return {"tenant": lambda: ctx.deps.user.username}

    toolset = Tenanted({"approve": _conditioned_spec(lambda tenant: tenant == "acme")})

    assert "approve" in await toolset.get_tools(ctx_for(User(username="acme")))
    assert "approve" not in await toolset.get_tools(ctx_for(User(username="globex")))


async def test_a_condition_is_asked_on_the_toolsets_own_dispatch_thread():
    """``thread_sensitive`` and ``executor`` govern the listing as they do the call.

    A condition stands in front of the call it gates, so a deployment that moves
    dispatch off asgiref's shared thread moves its conditions with it.
    """
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
    """The dispatch's connection cleanup, owed by the listing's hop as well.

    A condition that queries opens a connection on asgiref's shared thread,
    and off HTTP nothing closes it. Left open, it would also disarm the
    dispatch's own cleanup: that closes only what *it* opened, and a connection
    already open when a dispatch starts reads as someone else's. So this lists,
    checks, then dispatches and checks again.
    """
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
    """Nothing new on the call path: an omitted name is simply not a tool.

    pydantic-ai answers it with a retry naming the tools that do exist, and the
    service never runs.
    """
    ran: list[bool] = []
    retries: list[str] = []

    def approve(**_: Any) -> None:
        """Approve."""
        ran.append(True)

    def calls_approve(messages: Any, info: Any) -> Any:
        retry = [p for p in messages[-1].parts if isinstance(p, RetryPromptPart)]
        if retry:
            retries.append(retry[0].model_response())
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
    """On a selector nothing validates the spread, so reservation is the guard.

    The model names ``tenant`` itself and gets the project's value back: the
    registry is what tells dispatch the name is the transport's, and it reaches
    dispatch only if the toolset hands it over. Without it the model's
    ``globex`` is what the selector reads -- a caller choosing the tenant it is
    scoped to. What becomes of the argument where the spec does not declare
    the name cannot be shown here, since this selector declares ``tenant``;
    ``test_a_seed_named_argument_a_spec_never_declares_is_refused``
    holds that half.
    """

    def tenant_rows(tenant: str) -> list[dict[str, str]]:
        """The caller's tenant, as a one-row collection."""
        return [{"tenant": tenant}]

    toolset = SpecToolset(
        {
            "rows": SelectorSpec(
                kind=SelectorKind.LIST,
                selector=tenant_rows,
                # Taken off the selector's signature, as a library compiling the
                # spec marks it: a reflected read named like a registered seed is
                # the seed's to fill.
                reads=Parameters.of(Parameter("tenant", "string", reflected=True)),
                presenter=RowPresenter(OutputField("tenant", "string")),
                permissions=allow(),
            )
        },
        pool_seeds=_tenant_seeds(),
    )

    result = await _call(toolset, "rows", User(username="acme"), {"tenant": "globex"})

    assert _rows(result) == [{"tenant": "tenant-of-acme"}]
    # Not advertised either: a reflected read a registered seed fills is the
    # server's, so the model is never offered an argument it cannot set.
    # Holds the registered half of ``pool_seeds.reserved`` in the
    # ``server_owned_keys`` ``_caller_schema`` takes out.
    schema = toolset._tool_defs["rows"].parameters_json_schema
    assert "tenant" not in schema.get("properties", {})
    assert "tenant" not in schema.get("required", [])


def test_a_shape_a_schema_fragment_drops_is_told_nothing():
    """A ``metadata["json_schema"]["input"]`` fragment wins key by key, so one
    carrying its own ``properties`` can leave a declared shape out of the
    schema, and the scope sentence a list tool adds to its shapes is not
    written for a property that is not there. Holds the
    ``shapes & properties.keys()`` in ``_with_scope``: scoping every shape
    would fail building this tool on a ``fields`` it no longer lists."""
    spec = list_spec(
        shapes=Parameters.of(Parameter("fields", "string")),
        metadata={"json_schema": {"input": {"properties": {}}}},
    )

    schema = _input_schema_of(SpecToolset({"rows": spec}), "rows")

    assert list(schema["properties"]) == ["page", "limit"]


async def test_a_validator_field_named_like_a_seed_is_the_model_s_to_send():
    """The field is validated into ``data``; the service's ``tenant`` stays the seed's.

    Stripping every seed-named argument the spec declares would drop the
    model's value before the Validator saw it, and the required field would
    then refuse the call. The field's value and the seed's differ here, so the
    result shows each reached its own seat.
    """

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

    assert result == [
        {"destination": "globex", "tenant": "tenant-of-acme"},
        {"destination": "initech", "tenant": "tenant-of-acme"},
    ]


def _books_seeds() -> PoolSeeds:
    """Books are open for the user named ``open`` and nobody else."""
    return DEFAULT_POOL_SEEDS.extend(books_open=lambda user: user.username == "open")


@pytest.mark.parametrize(("username", "listed"), [("open", True), ("closed", False)])
async def test_a_condition_reading_a_registered_seed_is_asked_with_it(username, listed):
    """The listing and the call ask the condition against the same seeds.

    The listing's pool needs the registry twice over -- resolved into the pool
    by ``base_pool(seeds=)``, and admitted into what the condition is shown by
    ``unmet_operation_affordance(reserved=)`` -- and dropping either one makes
    the condition fail to bind here rather than answer. The call's own
    enforcement is the kernel's ``dispatch(pool_seeds=)``, asked for the user it
    lists.
    """
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


async def test_a_seed_named_argument_a_spec_never_declares_is_refused():
    """A registered seed's name is not an argument of a spec that never reads it.

    The toolset drops the model's value of a seed's name only where the spec
    declares the name, so the seed answers in its place. Here nothing declares
    it, so under the default ``REJECT`` the kernel refuses it as it refuses any
    undeclared key, registry or no registry.
    """

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
    """``user`` is the dispatcher's, so a selector reading it is never handed the model's.

    No schema advertises it, and the kernel declares no parameter of its name,
    so a model sending it anyway sends an undeclared key: ``REJECT`` refuses
    it, and ``IGNORE`` drops it and the selector still reads the caller.
    """

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
    """A Validator's ``unsendable`` names the required fields it declares no
    parameter for, so the kernel refuses the model's value for one as an
    unknown argument under ``REJECT`` and drops it under ``IGNORE``, and the
    Validator then fails the call for the missing field. So the toolset refuses
    the spec when it is built, under either policy, naming the tool and the
    field. Read duck-typed, naming no package, so any Validator answering it is
    read alike."""
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
    """A Validator without the attribute names no field, so the tool builds
    and serves: the attribute is optional, and its absence is "none"."""
    toolset = SpecToolset({"note": _note_spec(django_service_specs.DataclassValidator(_NoteInput))})

    assert await _call(toolset, "note", User(username="u"), {"text": "hi"}) == {"text": "hi"}


async def test_an_empty_answer_builds_and_serves():
    """An empty ``unsendable`` refuses nothing."""

    class Sendable(django_service_specs.DataclassValidator):
        unsendable: frozenset[str] = frozenset()

    toolset = SpecToolset({"note": _note_spec(Sendable(_NoteInput))})

    assert await _call(toolset, "note", User(username="u"), {"text": "hi"}) == {"text": "hi"}


def test_a_partial_service_is_not_refused_for_an_unsendable_field():
    """A partial call may leave any field out, so a field no call can send is
    left out as any other is and the tool builds. The attribute names the field
    whether or not the spec is partial, so the toolset reads the compiled
    spec's ``partial`` itself. Holds the ``partial`` arm of
    ``_refuse_unsendable_fields``."""
    toolset = SpecToolset(
        {"note": _note_spec(_PartialUnsendableValidator(_NoteInput), partial=True)}
    )

    assert "note" in toolset._tool_defs


@pytest.mark.parametrize("declare", [shaping, reading], ids=["shape", "read"])
def test_a_channel_named_after_a_registered_seed_is_refused(declare):
    """A registered seed is reserved like a built-in one, so a channel cannot name it.

    Accepted, the tool would build and be advertised, and the kernel would
    refuse every call to it, sent or not, for an argument meeting a seed in one
    pool.
    """
    with pytest.raises(ImproperlyConfigured, match="tenant"):
        SpecToolset(
            declare({"list": list_spec()}, {"list": [Parameter("tenant", "string")]}),
            pool_seeds=_tenant_seeds(),
        )


@pytest.mark.parametrize("seat", ["reads", "shapes"])
def test_a_mapping_read_named_after_a_registered_seed_is_refused_at_build(seat):
    """What a registry-built tool is refused at build
    (``test_a_channel_named_after_a_registered_seed_is_refused``), a tool built
    from a bare mapping is refused too: the check reads the compiled spec's own
    reads and shapes, which a mapping's spec carries as a registry's does."""
    spec = replace(list_spec(), **{seat: Parameters.of(Parameter("tenant", "string"))})

    with pytest.raises(ImproperlyConfigured, match=r"^SpecToolset tool 'list': .*'tenant'"):
        SpecToolset({"list": spec}, pool_seeds=_tenant_seeds())


def test_a_lookup_read_named_after_a_registered_seed_is_refused_at_build():
    """A read the target lookup takes is declared on the lookup's reads, not the
    service's, and is checked there too: the kernel refuses it on every call
    as it does a top-level one."""

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


# --- what a call needs from the model -----------------------------------------
#
# A tool's input schema is the model's only account of what to send. A spec
# declares the reads its callables take, and the toolset takes out the names the
# server fills: those are not advertised, every other read without a default is
# required, and a call missing one is handed back as a retry instead of reaching
# the callable as a ``TypeError``.


class _NameSerializer(RowPresenter):
    """A row as its ``name``: the presenter every name-only spec here renders with.

    A kernel ``Presenter``, kept under the name the other suites import it by;
    a spec takes an instance, ``presenter=_NameSerializer()``.
    """

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
    """``get_widget(user, pk)`` cannot run without ``pk``, which its spec declares
    as a required read, and only the model sends it."""
    schema = _input_schema_of(SpecToolset({"get": retrieve_spec()}), "get")

    assert schema["properties"] == {"pk": {"type": "integer"}}
    assert schema["required"] == ["pk"]


def recent_names(user, progress, newest_first: bool = True):
    """The acting user's names, newest first unless told otherwise."""
    names = ["older", "newer"]
    return [{"name": name} for name in (reversed(names) if newest_first else names)]


async def test_a_dispatcher_seed_is_neither_advertised_nor_required():
    """``progress`` is the dispatcher's own: always in the pool, never the model's.

    The selector takes it with no default, and the spec declares no read of
    it, since the kernel refuses a read of a name dispatch seeds: the pool
    hands it over, and the model is neither shown it nor asked for it.
    """
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
    # The kernel states a declared default, so the model reads what an omitted
    # value means.
    assert schema["properties"]["newest_first"] == {"type": "boolean", "default": True}
    # ``newest_first`` has a default, so nothing is required at all.
    assert "required" not in schema
    assert _rows(result) == [{"name": "newer"}, {"name": "older"}]


def names_for_tenant(tenant):
    """The names in the caller's tenant."""
    return [{"name": tenant}]


async def test_a_seed_parameter_the_model_leaves_out_is_not_refused():
    """A registered seed has no default, and the model is never asked for it,
    because the pool always carries it.

    Holds ``pool_seeds`` reaching ``_required_arguments`` from ``_call_spec``:
    told only the dispatcher's own seeds, the check would require ``tenant`` of a
    model that was never shown it, and refuse this call.
    """
    toolset = SpecToolset({"names": _name_list_spec(names_for_tenant)}, pool_seeds=_tenant_seeds())

    result = await _call(toolset, "names", User(username="acme"))

    assert _rows(result) == [{"name": "tenant-of-acme"}]


def names_in_project(user, project_pk):
    """The names in one project."""
    return [{"name": f"project-{project_pk}"}]


@pytest.mark.parametrize("source", ["registry", "mapping"])
async def test_a_url_kwarg_answers_for_the_parameter_it_fills(source):
    """A read with a default fills the parameter whenever the model leaves it
    out, so the model is not asked for it, from a registry and from a bare
    mapping alike.

    Holds the declaration ``_caller_schema`` reads, and the reads ``_call_spec``
    hands ``_fill_reads``: the kernel hands a callable nothing it was not sent,
    so nothing else applies the default.
    """
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
    """The call applies a read's default only where the model sent nothing,
    whichever way the spec arrived."""
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
    """A ``null`` sent under a read is the model declining to fill it, as under
    a shape, so the read's default answers, from a registry as from a mapping:
    both hand the toolset the same read."""
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
    """A required read sent as ``null`` is the model declining a value
    it must send, so the kernel's check retries it, in the kernel's words,
    whichever way the spec arrived: neither source pops the read off the call
    before the kernel sees it. The guide's ``spec-registry`` page states the
    wording."""
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
    """With no default the value reaches the selector only when the model sends
    it, so a read declared required is required of the model, and a call
    without it is handed back rather than reaching the selector as a
    ``TypeError``.

    Holds ``_required_arguments``' reading of the schema's ``required``: a
    pre-check blind to it would hand the call to the selector without
    ``project_pk``.
    """
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
    """An override is code the schema cannot read, so the name it fills, a read
    taken off the selector's signature, is a parameter without a default like
    any other, advertised as required. The
    call itself is served: the override is asked before the arguments are
    checked, so a call leaving the name out reaches the seed, and one sending it
    is served the seed's value over its own, told nothing. Leaving the name
    out of the reads is the remedy, which the next test holds."""
    toolset = _ProjectScopedToolset({"in_project": _in_project_reflecting()})

    schema = _input_schema_of(toolset, "in_project")
    left_out = await _call(toolset, "in_project", User(username="u"))
    sent = await _call(toolset, "in_project", User(username="u"), {"project_pk": 5})

    assert schema["required"] == ["project_pk"]
    assert _rows(left_out) == [{"name": "project-42"}]
    assert _rows(sent) == [{"name": "project-42"}]


def _in_project_reflecting() -> SelectorSpec:
    """``names_in_project``, its ``project_pk`` read as a library compiling it
    reflects a parameter with no default off the signature."""
    return _name_list_spec(
        names_in_project,
        reads=Parameters.of(Parameter("project_pk", "integer", required=True, reflected=True)),
    )


def names_in_hidden_project(user, project_pk: int):
    """``names_in_project``, whose spec declares no read of ``project_pk``."""
    return [{"name": f"project-{project_pk}"}]


async def test_a_name_a_call_seeds_override_fills_is_kept_from_the_model():
    """What ``call_seeds``' docstring tells an override to do first: leave the
    name out of the spec's reads. It is then out of the schema and out of the
    input set a call is checked against, so the model is never told about
    ``project_pk``, a call leaving it out reaches the override, which fills
    it, and a call sending it anyway is handed back as an unexpected argument,
    rather than served another project's rows in silence as a declared
    parameter is in the test before.
    """
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
    """Under ``IGNORE`` the undeclared name is not refused, and the model's value
    is dropped before the selector reads its arguments, so the override's is the
    one served.
    """
    toolset = _ProjectScopedToolset(
        {"in_project": _name_list_spec(names_in_hidden_project)},
        unknown_arguments=UnknownArguments.IGNORE,
    )

    result = await _call(toolset, "in_project", User(username="u"), {"project_pk": 5})

    assert _rows(result) == [{"name": "project-42"}]


async def test_a_marked_name_sent_to_an_open_selector_is_refused():
    """A selector's bare ``**filters`` does not open the argument set: the kernel
    closes it at the declared parameters, and an undeclared name is not one, so under
    ``REJECT`` the model's value is refused as on a closed selector."""
    toolset = _ProjectScopedToolset(
        {"in_project": _name_list_spec(names_in_hidden_project_or_filtered)}
    )

    with pytest.raises(ModelRetry) as raised:
        await _call(toolset, "in_project", User(username="u"), {"project_pk": 5})

    assert str(raised.value) == "project_pk: Unknown argument."


async def test_a_name_a_call_seeds_override_fills_is_declared_to_the_toolset():
    """The declaration to the toolset that keeps a name out of the schema too: a
    seed the mount registers, which the tool's schema reads, where an override's
    is code it cannot. The name is not advertised, a call leaving it out is
    filled by the resolver, and a value the model sends is not passed on."""
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
    """A declared read is one the model sends, and a seed of its name would
    stand over the model's value in silence -- a model asking for project 5
    served project 42's rows. The kernel refuses an argument meeting a seed in
    one pool, so the call fails as the configuration error it is."""
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
    """The kernel reads a provider's ``TypedDict`` return annotation as the keys
    it fills, so a provider annotated with one says which parameters it fills."""
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
    """A provider returning an undeclared ``dict`` may fill any parameter, so the
    toolset drops none of the reads beside it: ``ceiling``, declared optional,
    stays advertised, since the model may still be the one to send it, and a
    call leaving it out reaches the provider, which fills it.
    """
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


def test_a_marked_parameter_stays_required_beside_an_untyped_provider():
    """A read declared required stays required beside a provider whose keys
    cannot be read, while the optional one beside it stays optional.

    Holds ``_caller_schema`` leaving an untyped seat as declared: dropping
    every requirement there would leave ``floor`` optional.
    """
    toolset = SpecToolset(
        {
            "band": _under(
                names_in_band, _FLOOR, kwargs=_ceiling_from_project, ceiling=_OPEN_CEILING
            )
        },
    )

    assert _input_schema_of(toolset, "band")["required"] == ["floor"]


def _untyped_band(view):
    """Both ends of the band, with nothing declared about the keys."""
    return {"ceiling": 10, "floor": 2}


async def test_an_untyped_provider_filling_a_marked_parameter_is_not_refused():
    """The schema keeps ``floor`` required, as its marker says, but the call is
    not checked for it: the provider may be what fills it, as it is here, and only
    the assembled pool can say, which the kernel checks the declaration against.

    Holds the ``may_fill`` subtraction in ``_required_arguments``: refusing
    what the schema requires would refuse this call for ``floor``.
    """
    toolset = SpecToolset(
        {"band": _under(names_in_band, _FLOOR, kwargs=_untyped_band, ceiling=_OPEN_CEILING)}
    )

    result = await _call(toolset, "band", User(username="u"))

    assert _rows(result) == [{"name": "cheap"}]


def test_a_required_read_its_default_fills_is_not_required_beside_an_untyped_provider():
    """The declaration requires ``floor``, and its default fills it on every
    call, so the model is not asked for it: what the declaration requires is
    kept only where the toolset does not fill the name.
    """
    floor = Parameter("floor", "integer", required=True, default=2)
    toolset = SpecToolset(
        {"band": _under(names_in_band, floor, kwargs=_untyped_band, ceiling=_OPEN_CEILING)}
    )

    assert "floor" not in _input_schema_of(toolset, "band").get("required", [])


def _unresolvable_ceiling(project_pk=None) -> _NotDeclaredAnywhere:  # noqa: F821
    """A provider whose return annotation names something that does not exist."""
    return _ceiling_from_project(project_pk)


def test_a_provider_whose_annotation_cannot_be_resolved_counts_as_untyped():
    """An annotation that cannot be read says nothing about the keys, so the
    provider may fill anything, as an unannotated one may: ``ceiling`` is not
    dropped as filled, and, declared optional as beside any provider that may
    fill it, the toolset requires nothing."""
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
    """``ceiling`` always, ``currency`` when the provider has one.

    Totality rather than ``NotRequired``, which this module's postponed
    annotations hide from the ``TypedDict`` machinery.
    """

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
    """``currency`` always, ``ceiling`` or ``UNSET`` to leave it to the caller.

    ``currency`` is a union as well, ``str | None``, so that a key whose
    annotation merely has arguments is told apart from one that admits
    ``UnsetType``: both are unions, and only ``ceiling`` may be declined.
    """

    ceiling: int | UnsetType
    currency: str | None


def _scope_or_decline(view) -> _ScopeOrDecline:
    return _ScopeOrDecline(ceiling=UNSET, currency="EUR")


# ``_ScopeOrDecline`` with the declinable key wrapped in ``NotRequired``, which
# puts ``UnsetType`` one level further down wherever ``get_type_hints`` leaves
# the wrapper in place: on Python 3.10, whose ``typing`` predates
# ``NotRequired`` and so does not strip ``typing_extensions``' one. From 3.11
# the wrapper is stripped and the key reads as ``int | UnsetType`` again. The
# functional syntax because this module's postponed annotations hide
# ``NotRequired`` from the ``TypedDict`` machinery, as ``_Scope`` notes.
_WrappedScopeOrDecline = TypedDict(  # noqa: UP013
    "_WrappedScopeOrDecline",
    {"ceiling": NotRequired[int | UnsetType], "currency": str},
)


def _wrapped_scope_or_decline(view) -> _WrappedScopeOrDecline:
    return {"ceiling": UNSET, "currency": "EUR"}


class _ScopeWithAnUnresolvableKey(TypedDict):
    ceiling: _NotDeclaredAnywhere  # noqa: F821


def _scope_with_an_unresolvable_key(view) -> _ScopeWithAnUnresolvableKey:
    return {"ceiling": 10}


def _scope_for_an_unresolvable_view(view: _NotDeclaredAnywhere) -> _Scope:  # noqa: F821
    """A readable return annotation beside a parameter annotation that is not,
    as an import made only under ``TYPE_CHECKING`` leaves one."""
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
    """Relaxing the one required read a provider may decline leaves no
    ``required`` at all, as the kernel writes none for a spec requiring
    nothing, rather than an empty list. Holds the ``if required`` in
    ``_caller_schema``."""
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
        (_wrapped_scope_or_decline, ["ceiling", "owner", "region"], ["owner", "region"]),
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
        "wrapped-declinable-key",
        "unresolvable-key",
        "unresolvable-parameter",
    ],
)
def test_a_providers_return_annotation_decides_what_the_model_is_asked_for(
    provider, asked, required
):
    """A spec with no provider fills nothing, which is not the "cannot tell" of
    one whose keys are not declared: the first leaves every read without a
    default required, the second none, its reads declared as a reflecting
    compile leaves them beside such a provider, and the toolset adds no
    requiredness of its own. A typed provider's keys are dropped from the
    schema, since it fills them. A ``NotRequired`` key is the provider's
    as much as a required one, and a parameterised ``TypedDict`` keeps its keys
    on its origin, where an alias does not relay them.

    A key annotated to admit ``UNSET`` may be declined, which leaves the caller's
    value through, so it stays advertised and is not required, while the same
    provider's other keys are still not asked for, ``currency: str | None``
    included: a union is not a decline. A ``NotRequired`` around the key, which
    Python 3.10's ``typing`` leaves in place, changes nothing.

    One annotation that does not resolve costs only what it names. A key whose
    own annotation does not resolve might be one the provider declines, so
    ``ceiling`` stays offered beside ``_scope_with_an_unresolvable_key`` while
    the parameters nothing fills stay required; a parameter's annotation says
    nothing about the keys, so ``_scope_for_an_unresolvable_view`` reads as
    ``_scope`` does. Neither row reads the whole provider as untyped, which
    would offer every name and require none.

    The reader is the kernel's ``provider_keys``; this holds what the toolset
    makes of its answer, on the schema the model reads.
    """
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
    """``selector``'s keyword parameters as a library compiling it reflects them:
    each required, since none has a default, and typed loosely, since only the
    names are under test."""
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
    """A provider cannot return ``UNSET`` for a key whose value is a list or a dict,
    so ``regions`` and ``labels`` are always filled, and the provider's value would
    replace whatever the model sent for them. The key is the provider's, and the
    tool does not ask for it.

    Read as declinable because ``UnsetType`` appears somewhere inside the
    annotation, both were advertised.
    """
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
    """Neither annotation hides which keys the provider returns, so ``pk``, which
    nothing fills, stays required. A parameter's type says nothing about the
    keys, and ``tenant`` is filled. A key whose own value does not resolve might
    be one the provider declines, so ``tenant`` stays offered, optional.

    One unresolvable annotation does not make the whole provider untyped, which
    would offer the model ``tenant`` beside the first and require ``pk`` of
    neither.
    """
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
    """``-> _GenericTenant[str | UnsetType]`` says what the written-out
    ``tenant: str | UnsetType`` says, so the model is offered ``tenant`` for the
    calls the provider declines it on.

    Read as the bare type variable, ``tenant`` counted as filled through the
    generic, and a decline left the call without it.
    """
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
    """The kernel drops a provider key returned as ``UNSET`` from the pool,
    which lets the caller's value through, so a key annotated to allow that is
    not one the toolset fills. It stays advertised, since the model may be the
    one to send it, and optional, since the provider may fill it instead.

    Counted as filled, it would be dropped from the schema, so a model never
    told about ``ceiling`` would leave it out and a decline would reach the
    selector as ``TypeError``. Holds the declinable keys in ``_caller_schema``:
    left required as declared, the ``filled`` call would be refused for
    ``ceiling``.
    """
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
    """What the quickstart and the changelog say happens when a provider that may
    fill a parameter does not, by declining it with ``UNSET`` or, untyped, by
    leaving it out, and the model has not sent it either. The toolset does not
    check such a parameter before the call, since only the assembled pool can say
    whether it arrived, and the kernel checks that pool: the caller's value for
    a declined read stays beneath the provider's answer, so the model could have
    filled ``ceiling``, and the call comes back as a retry naming it rather than
    reaching the selector, in the kernel's words for a missing field,
    ``ceiling: This field is required.``.

    The retry, not the selector's ``TypeError`` out of the run, is what a model
    can act on: a resend carrying ``ceiling`` is served, and a default still answers the
    call without one.
    """
    # A provider annotated to decline ``ceiling`` relaxes the required read; an
    # untyped one cannot be read, so its author declares the read optional.
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

    # The kernel's own refusal, keyed by the name, not the toolset's
    # ``missing_arguments`` sentence: only the pool the kernel assembles can
    # say the parameter went unfilled, so the toolset's pre-dispatch check never
    # sees it. The negative assertion holds which of the two answered.
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
    """The model was told about ``name`` and ``price`` and never about ``pk``, the
    one argument saying which row to change. The kernel hands the instance
    selector its reads from the same arguments, so they are advertised beside
    the Validator's fields, with the names the toolset fills left out: the
    ``tenant`` read the registered seed answers.
    """
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
    """``additionalProperties: false`` says an undeclared argument is refused,
    which is true under ``REJECT`` and false under ``IGNORE``, where the call
    drops one and is served. So the schema states it under the toolset's own
    policy, at the top and on each row of a list alike."""
    toolset = SpecToolset(
        {"list_widgets": list_spec(), "create_widgets": bulk_spec()}, unknown_arguments=policy
    )

    listed = _input_schema_of(toolset, "list_widgets")
    bulk = _input_schema_of(toolset, "create_widgets")

    assert listed.get("additionalProperties") is closed
    assert bulk.get("additionalProperties") is closed
    assert bulk["properties"]["items"]["items"].get("additionalProperties") is closed


def test_a_service_without_a_lookup_advertises_the_kernel_s_schema_as_it_is():
    """No lookup and nothing the server fills: a service's schema is the
    kernel's schema of the compiled spec, byte for byte, down to a service whose
    callable takes nothing from the model advertising an object that admits no
    argument at all."""
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
    """The kernel reads a Validator field and a target lookup's read of one name
    as one argument declared twice, and refuses the spec when its argument set
    is assembled. That is while the toolset is built, so the refusal names the
    tool, rather than at the tool's first call."""
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
    """A lookup read declared optional is advertised and left optional, and a
    Validator whose fields are all optional requires nothing either, so the
    tool requires nothing."""
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
    """Holds the target-lookup seat in ``_read_seats``, a collection lookup's included."""
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
    """The kernel refuses a target lookup dispatch would never call when the
    spec is built: an instance lookup beside a collection one, and any lookup
    beside ``many=True``. ``_read_seats`` relies on it, and so has no
    precedence between the two lookups and no ``many`` arm: neither spec reaches
    a toolset to be asked about.
    """
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
async def test_a_serializer_less_service_advertises_what_dispatch_admits():
    """A service with no Validator is dispatched under the toolset's
    argument binding, ``AUTO``, which binds a service ``BUNDLE``: ``REJECT``
    admits its target lookup's keys and none of its own parameters. The schema
    is read under the same binding, so it lists ``pk`` and not ``note``, which a
    ``SPREAD_*`` reading would list and this dispatch refuses.
    """
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
    """Handed back, not passed to the selector to raise ``TypeError`` out of the run.

    A retry naming every missing argument, so one turn fixes them all.
    """
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
    """A provider's values reach its own callable's seat only, so a key the
    service's ``kwargs=`` may decline says nothing about the target lookup
    reading the same name: the lookup cannot run without ``pk`` from the caller,
    so the schema requires it and a call leaving it out is handed back.

    Holds the ``names &`` in ``_caller_schema``: relaxing every key a provider
    may decline, whatever its seat, would leave ``pk`` optional.
    """
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


@pytest.mark.django_db(transaction=True)
async def test_a_service_call_missing_its_lookup_and_its_fields_hears_about_each_in_turn():
    """The check before the call names the lookup's reads, not the Validator's
    fields, which the Validator reports once the call runs. So a
    call missing both is told about ``pk`` alone, and about the fields on the
    turn after, which is what the docs and the class docstring say."""
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
    """What the retry is for: a run that reaches the row on the second call
    rather than aborting on the first."""
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
    """A ``kwargs=`` provider declaring that it fills ``status``, from a read it
    takes by name, is the ordinary way to route a caller's argument to a
    selector under another name, and nothing is lost: the model's value reaches the
    provider as an argument and the provider hands it to the selector. The
    provider owns ``status``, so the model is not offered it as the selector's.

    The two names differ on purpose. A provider filling the very name it reads
    is never handed the model's value of it: a key a typed provider fills is
    the provider's, so the model's is dropped, and the provider's default answers.
    """
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
    """The limit of reading by name, held so a change to it is seen: the model's
    ``status`` is dropped as a key the provider owns, so the provider is handed
    its own default, and the call serves ``open`` whatever the model sent."""
    toolset = SpecToolset(
        reading(
            {"filed": _name_list_spec(names_filed_under, kwargs=_status_from_itself)},
            {"filed": [Parameter("status", "string")]},
        )
    )

    result = await _call(toolset, "filed", User(username="u"), {"status": "closed"})

    assert _rows(result) == [{"name": "draft"}]


def test_a_read_the_provider_fills_is_not_advertised():
    """The schema says what the call above does: ``status`` is a key the typed
    provider fills, so the kernel's ``server_owned_keys`` names it and the model
    is not offered an argument whose value is dropped."""
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
    """``limit`` rows and nothing at all about the rest would have a model answer
    "there are 2 widgets" from a page of 2 out of 5 -- confidently, and with no
    way to find out otherwise.
    """
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
    """The kernel clamps rather than serving an empty page at a huge OFFSET.

    The clamp is the part that has to be visible: a model asking for page 99 and
    silently receiving page 2 would read the rows as the ninety-ninth page.
    """
    user = User.objects.create(username="u")
    for n in range(3):
        Widget.objects.create(name=f"w{n}", price=n, owner=user)

    result = _dispatch(list_spec(), user, {"limit": 2, "page": 99})

    assert result["page"] == 2
    assert result["totalPages"] == 2
    assert result["hasNext"] is False


@pytest.mark.django_db
def test_an_empty_collection_is_one_empty_page():
    """Not zero pages: ``page`` is 1-based and the page served is 1, so reporting
    zero would describe the page the caller was just handed as not existing."""
    user = User.objects.create(username="u")

    result = _dispatch(list_spec(), user, {})

    assert result == {"items": [], "page": 1, "totalPages": 1, "hasNext": False}


@pytest.mark.django_db
def test_the_envelope_carries_projected_rows_and_is_not_itself_projected():
    """The projection lands on the rows; the envelope's keys belong to no
    presenter, so a projection walking them would look for markings that cannot
    exist."""
    user = User.objects.create(username="u")
    widget = Widget.objects.create(name="Sprocket", price=100, owner=user)

    result = _dispatch(agent_list_spec(), user)

    assert _rows(result) == [{"id": widget.pk, "name": "Sprocket", "status": "In stock"}]
    assert result["page"] == 1


@pytest.mark.django_db
def test_the_byte_ceiling_is_measured_on_the_envelope():
    """What is sent is what is measured. The envelope is small, but a ceiling
    computed on the unwrapped rows would be a bound on something else."""
    user = User.objects.create(username="u")
    Widget.objects.create(name="a", price=1, owner=user)

    with pytest.raises(ToolFailed) as caught:
        _dispatch(list_spec(), user, {}, max_result_bytes=40)

    assert "40 byte ceiling" in caught.value.message


async def test_the_pagination_instruction_teaches_the_envelope():
    """A model told to send ``limit`` and not told what comes back reads
    ``items`` as the whole collection."""
    instructions = await SpecToolset({"list": list_spec()}).get_instructions(None)

    assert instructions is not None
    assert '"items"' in instructions
    assert "hasNext" in instructions


async def test_the_page_argument_no_longer_claims_to_require_limit():
    """It never did anything on its own before, because an omitted ``limit``
    meant "everything" and the slice was skipped. Now every call is a page."""
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
    """Read off the schema, then checked against what a call with no ``limit`` gets.

    The description was a module-level string formatted with
    ``DEFAULT_PAGE_SIZE``, so a toolset with ``max_page_size=3`` told the model
    "Defaults to 100" beside ``maximum: 3`` while serving three rows -- a
    default the tool could not serve, next to a ceiling contradicting it.
    """
    user = await User.objects.acreate(username="u")
    for n in range(5):
        await Widget.objects.acreate(name=f"w{n}", price=n, owner=user)
    toolset = SpecToolset({"list": list_spec()}, max_page_size=3)
    tools = await toolset.get_tools(ctx_for(user))
    limit = tools["list"].tool_def.parameters_json_schema["properties"]["limit"]

    # One toolset answers both questions, through ``call_tool``: the ceiling
    # reaches the slice from the constructor, as it does in a run.
    served = _rows(await _call(toolset, "list", user))

    assert limit["maximum"] == 3
    assert _stated_default(limit["description"]) == len(served) == 3


async def test_a_ceiling_above_the_default_page_size_leaves_the_stated_default_alone():
    """The served default is the smaller of the two, not the ceiling.

    ``paginate_output`` clamps an omitted ``limit`` *down* to the ceiling and
    never raises it, so a toolset allowing 500 rows a page still serves 100
    to a call naming none. Stating the ceiling as the default would be the same
    defect in the other direction.
    """
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
    """The conventions block carried the same module-level number.

    It is the toolset-wide statement of the same contract, and a model reading
    "default 100" in its instructions and "Defaults to 3" on the tool has been
    told two things.
    """
    instructions = await SpecToolset({"list": list_spec()}, max_page_size=3).get_instructions(None)

    assert instructions is not None
    assert "`limit` (items per page, default 3)" in instructions
    assert f"default {DEFAULT_PAGE_SIZE}" not in instructions


async def test_a_step_leaving_a_tool_out_states_the_same_default():
    """The per-step block is derived on its own path, so it needs the ceiling too.

    A step whose catalog omits an operation rebuilds the conventions from the
    tools it does offer, and that derivation is a second call site that could
    fall back to ``DEFAULT_PAGE_SIZE`` while the cached full block did not.
    """
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
    """The two halves generated from one spec, compared against each other.

    This is the assertion the release exists for: the schema claimed a
    pagination envelope and the payload was a bare list.
    """
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
    """``paginate`` asks *does this toolset wrap the result*, and for anything
    but a list selector the answer is no."""
    tools = await SpecToolset({"get": retrieve_spec()}).get_tools(None)
    return_schema = tools["get"].tool_def.return_schema

    assert return_schema is not None
    assert set(return_schema["properties"]) == {"id", "name", "price"}


@pytest.mark.parametrize(
    ("allow_none", "root_type"), [(True, ["object", "null"]), (False, "object")]
)
async def test_a_retrieve_tool_says_whether_it_can_return_null(allow_none, root_type):
    """An ``allow_none`` retrieve that finds nothing returns ``None``, which a
    schema declaring an object alone would deny.

    The ``False`` case holds ``spec.allow_none`` in ``_return_schema``: passed
    unconditionally, every retrieve tool would admit a ``null`` it never returns.
    """
    tools = await SpecToolset({"get": retrieve_spec(allow_none=allow_none)}).get_tools(None)
    return_schema = tools["get"].tool_def.return_schema

    assert return_schema is not None
    assert return_schema["type"] == root_type


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
    """dispatch materializes the re-read with ``.first()``, so a re-read that
    filters the row out hands the model ``None``, which an object schema with
    required fields refused. No declaration is needed: the re-read is one.
    """
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
    """With nothing to re-read, dispatch presents the service's own return, a
    ``None`` included. ``ServiceSpec(allow_none=True)`` is the declaration that it
    may, and the schema states it."""
    spec = ServiceSpec(
        service=touch_widgets,
        atomic=False,
        allow_none=True,
        presenter=widget_presenter(),
        permissions=allow(),
    )

    assert await _returned_against_its_schema(spec) == (None, ["object", "null"])


async def test_an_undeclared_none_is_handed_to_the_model_against_an_object_schema():
    """What the quickstart and the changelog say of a service that returns
    ``None`` with nothing to re-read and no ``allow_none=True``: dispatch still
    presents it, so the model is handed ``None``, and the schema, which states
    only what the spec declares, does not admit ``null``. The declaration is
    what admits it (``test_a_service_declaring_allow_none_admits_null``).
    """
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
            "array",
            id="list-reread",
        ),
        # A service presenting its own list hands back a ``None`` it returns
        # as ``None``, as a ``many`` service does, so ``allow_none`` admits it.
        pytest.param(
            ServiceSpec(
                service=touch_widgets,
                atomic=False,
                presenter=widget_presenter(),
                returns_many=True,
                allow_none=True,
                permissions=allow(),
            ),
            ["array", "null"],
            id="returns-many",
        ),
        # The kernel presents a ``many`` service's ``None`` return as ``None``,
        # so ``allow_none`` admits it around the whole array.
        pytest.param(bulk_spec(allow_none=True), ["array", "null"], id="many"),
    ],
)
def test_a_service_admits_null_only_where_dispatch_may_present_it(spec, root_type):
    """The reading is the kernel's ``can_present_nothing``, the answer every route
    states. A list re-read's result is empty rather than ``None``, with
    ``allow_none`` or without, so reading the flag for every service would
    promise ``null`` to the first two, which never return it. A ``many`` or
    ``returns_many`` service presents its own return, so a ``None`` from it is
    ``None``, which its ``allow_none`` declares.
    """
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


async def test_a_service_whose_output_is_a_list_is_an_array_not_an_envelope():
    """The toolset paginates list *selectors*. Claiming the envelope here would
    re-open the same schema-versus-payload gap one spec kind over. Declared
    ``returns_many``, dispatch presents the service's own return as the list,
    and the model is handed the bare array the schema states.
    """
    assert await _returned_against_its_schema(_presenting_a_list(touch_every_widget)) == (
        _TOUCHED,
        "array",
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The kernel's dispatch hands a returns_many service's return to the presenter "
        "when it is no list, where it should refuse it."
    ),
)
async def test_a_list_output_with_nothing_to_reread_refuses_a_single_row():
    """Declared ``returns_many``, the service's own return is the list presented,
    so a service returning one row is a configuration error raised out of the
    call, naming what the service returned, rather than one object handed to
    the model against a schema that promised an array.
    """
    with pytest.raises(ImproperlyConfigured, match=r"archive_widget returned dict"):
        await _call(SpecToolset({"op": _presenting_a_list(archive_widget)}), "op", None)


def test_a_spec_with_no_presenter_advertises_nothing():
    """``None`` is the correct answer, not a gap: a fabricated shape would be a
    claim the payload never has to honour."""
    spec = SelectorSpec(kind=SelectorKind.LIST, selector=list_widgets, permissions=allow())

    assert _return_schema(spec) is None
    assert _build_tool_def("list", spec).return_schema is None


async def test_the_return_schema_is_projected_like_the_payload():
    """A schema advertising a field the render drops is worse than no schema:
    the model asks for it, gets nothing back, and is told nothing about why."""
    toolset = SpecToolset({"widgets": agent_list_spec()})

    tools = await toolset.get_tools(None)
    item = tools["widgets"].tool_def.return_schema["properties"]["items"]["items"]

    assert "price" not in item["properties"]  # FieldMarking.hidden()
    assert item["properties"]["id"]["description"] == "Widget handle."


async def test_an_unlabelled_handle_gets_this_transports_own_wording():
    """The kernel supplies no fallback on purpose -- what to do with an
    identifier depends on the reader, and only the transport knows its audience.
    """
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
    """Populating it costs nothing; putting it on the wire costs context every
    turn. Pydantic-AI owns that opt-in at both scopes, so this stays ``None``.
    """
    tools = await SpecToolset({"list": list_spec()}).get_tools(None)

    assert tools["list"].tool_def.include_return_schema is None


async def test_a_consumer_can_opt_into_sending_the_return_schema():
    """The opt-in has to actually reach a schema that is there -- the wrapper
    only preserves what the tool def already carries.
    """
    toolset = SpecToolset({"list": list_spec()}).include_return_schemas()
    # The wrapper is Pydantic-AI's own, and it reads the context while listing
    # (newer releases dereference it unconditionally), so this needs a live
    # ``RunContext`` rather than the ``ctx_for`` double or ``None``.
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
    """A service whose output renders through a selector declaring ``rendered``.

    The nested selector hands the service's return straight back, and it has to
    exist: the answers are put on a row by the selector spec declaring them, so a
    nested spec with no selector renders a row that carries none.
    """
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
    """The kernel adds an ``affordances`` object to every item it presents for a
    spec declaring them, and the schema this toolset advertised never said so --
    a model reading the schema was told the key does not exist, then handed it.
    """
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
    """The two halves generated from one declaration, compared against each other
    through a real call rather than against expectations written here."""
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
    """``ServiceSpec.affordances`` are the conditions *that* service is checked
    against before it runs, and are never rendered. What a service's output
    carries is declared on the selector it renders through -- so the schema reads
    the same spec the render does, and here both say nothing.
    """
    spec = _order_service_spec(
        own=[Affordance(code="books_closed", reason="The books are closed.", when=lambda **_: True)]
    )
    toolset = SpecToolset({"orders": spec})
    # A real context rather than ``None``: the spec's own condition is asked
    # when the catalog is listed, against the user this context carries.
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
    """Rebuilt from the arguments the schema was built from before affordances
    were threaded through, and compared as JSON text so that key order counts
    too: a spec that declares none must not so much as gain an empty object.
    """
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
    """Through ``call_tool``, the path a run takes. The message carries the code
    beside the reason, and the cause is kept as well, because that is where a
    program reads the code from."""
    toolset = SpecToolset({"close_books": _refused_spec()})

    with pytest.raises(ToolFailed) as caught:
        await toolset.call_tool("close_books", {}, ctx_for(User(username="u")), None)

    assert caught.value.message == "The books are closed. (code: books_closed)"
    cause = caught.value.__cause__
    assert isinstance(cause, ActionUnavailable)
    assert cause.code == "books_closed"
    assert cause.message == "The books are closed."


async def test_a_refusal_names_the_code_its_rows_advertised():
    """The point of the suffix, asserted against the other half rather than a
    literal: the code a row's ``affordances`` answer carries for an operation is
    the code that operation's refusal ends with. Both come off one declaration,
    ``_CANCEL_ORDER``, so a model that read the row can connect the two."""
    toolset = SpecToolset(
        {
            "orders": _order_selector_spec(SelectorKind.RETRIEVE),
            "cancel": replace(_CANCEL_ORDER, permissions=allow()),
        },
        descriptions={"cancel": "Cancel an order."},
    )
    row = await _call(toolset, "orders", User(username="u"))
    advertised = row["affordances"]["cancel"]

    # Called directly: ``cancel``'s unmet condition leaves it out of the listing.
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
    """``ServiceConflict`` is ``ActionUnavailable``'s parent and is matched by the
    same branch, so it is the case a check written one class too wide would
    catch. A conflict raised by hand has no code, and must not gain a label."""

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
    """The consumer's map is consulted before any built-in arm, this one
    included, and it is where a program reads ``.code`` -- so a handler for the
    refusal, or for any class above it, sees the exception whole and decides the
    result without a suffix being added to it."""
    result = _dispatch(
        _refused_spec(),
        object(),
        toolset_kwargs={
            "exception_map": {registered: lambda exc: {"refused": exc.code, "why": exc.message}}
        },
    )

    assert result == {"refused": "books_closed", "why": "The books are closed."}


def test_translate_exception_sees_the_code_before_the_default_renders_it():
    """The override is the other door to the same place, and the one that can
    read the run: returning ``None`` for everything else leaves the suffix to the
    default, which the second call pins."""

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
    """The toolset takes no schema registry, so a filter's node is the kernel's
    derivation from the filter set, as ``spec_input_schema`` states it: a
    ``NumberFilter`` subclass is a decimal string, whatever type the project
    means by it. What is advertised is what the kernel's dispatch enforces."""
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
    """asgiref's default, and the reason the knob exists rather than a flip.

    ``thread_sensitive=True`` runs every call on asgiref's
    ``single_thread_executor``, which is a *class* attribute -- one thread shared
    by every toolset instance and every concurrent run in the process. That is
    what keeps Django's thread-local connections coherent, and it is also why a
    fan-out serialises.
    """
    toolset = SpecToolset({"echo": _thread_naming_spec()})

    names = await _call_concurrently(toolset, times=3)

    assert len(set(names)) == 1


@pytest.mark.django_db(transaction=True)
async def test_the_dispatch_thread_is_the_callers_to_choose() -> None:
    """With ``thread_sensitive=False`` and an executor, calls actually fan out.

    Pydantic-AI runs function tools in parallel within a segment, so a toolset
    that funnels them onto one thread turns a parallel step into a serial one.
    Asserted on thread identity rather than on elapsed time, which would make
    the suite a benchmark.
    """
    with ThreadPoolExecutor(max_workers=3) as pool:
        toolset = SpecToolset(
            {"echo": _thread_naming_spec()}, thread_sensitive=False, executor=pool
        )

        names = await _call_concurrently(toolset, times=3)

    assert len(set(names)) == 3


def _dispatch_thread_state() -> tuple[int, bool]:
    """This thread's ident, and whether it is holding an open default connection.

    Has to run *inside* the dispatch thread, because that is the only place the
    answer exists: ``django.db.connections`` is thread-local, so a caller can
    neither read nor close a connection another thread opened. That asymmetry is
    the whole shape of the bug -- it is also why a consumer cannot work around
    it from outside without pushing ``connections.close_all`` back into the same
    executor.
    """
    return threading.get_ident(), connections["default"].connection is not None


def _thread_recording_toolset() -> tuple[SpecToolset, dict[str, int]]:
    """A one-tool toolset whose selector records, in the dict beside it, its thread."""
    dispatched_on: dict[str, int] = {}

    def _list_recording_its_thread(user: Any) -> Any:
        """List every widget, recording which thread the dispatch ran on."""
        del user  # the subject is the connection, not the scoping
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
    """A dispatch must not leave a database connection open behind it.

    On the default ``thread_sensitive=True`` every call runs on asgiref's
    process-wide ``single_thread_executor``. Django opens a connection per
    thread and closes one per *request* -- and off HTTP there is no request, so
    nothing ever closes this one. It outlives the run, the toolset and the agent,
    and surfaces somewhere else entirely: at a test session's teardown as
    "database is being accessed by other users", naming neither this package nor
    the thread that holds the handle.

    Reading the state back through a second ``sync_to_async`` is not a
    convenience -- see ``_dispatch_thread_state``. The ident assertion is what
    keeps the probe honest: asgiref only guarantees one shared thread while
    ``thread_sensitive`` holds, and a probe that landed anywhere else would
    report a closed connection and pass for the wrong reason.

    Run at both ends of ``CONN_MAX_AGE`` because that setting is what separates
    ``close()`` from Django's own ``close_if_unusable_or_obsolete()``, and only
    the second parameter can tell them apart: at ``CONN_MAX_AGE=60`` the
    obsolescence check declines to close, the alias then reads as somebody
    else's on the next dispatch, and the leak is back. ``CONN_MAX_AGE`` budgets
    reuse *between requests*; this thread has none, so a connection held for the
    next request is held forever.
    """
    # Django's SQLite backend *ignores* ``close()`` on an in-memory database, to
    # avoid destroying it -- so under a ``:memory:`` test database this test
    # could not tell a working fix from a no-op, and a fix would come out green
    # without being verified. Assert the
    # environment can observe a close before asserting anything about one.
    _assert_a_close_is_observable()

    toolset, dispatched_on = _thread_recording_toolset()
    # Unsaved on purpose. Saving one would need a thread hop of its own -- the
    # ORM is closed to the event loop -- and a row written from the dispatch
    # thread commits on that thread's connection, outside the transaction this
    # test is rolled back with. Nothing here reads the user anyway.
    ctx = ctx_for(User(username="u"))
    tools = await toolset.get_tools(ctx)

    # One settings dict backs every thread's wrapper, so this reaches the
    # dispatch thread's connection as well as this one's. ``close_at`` is fixed
    # at connect time, so the close and reopen have to happen inside this
    # window or the setting would not reach the connection under test.
    settings_dict = connections.settings["default"]
    original_max_age = settings_dict["CONN_MAX_AGE"]
    settings_dict["CONN_MAX_AGE"] = conn_max_age
    try:
        # Whatever an earlier test left on the shared thread is not this test's
        # subject. Start from a closed connection and prove that took, so the
        # closing assertion is about this dispatch and nothing else.
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
    """The other half, and the one an unconditional cleanup gets wrong.

    ``sync_to_async(thread_sensitive=True)`` does *not* always mean another
    thread: with a synchronous frame above the loop -- a WSGI request driving an
    agent through ``async_to_sync``, which is what this test builds -- asgiref
    routes the dispatch straight back onto the caller's own thread. That
    thread's connection belongs to the request and is very likely mid-``atomic``,
    so a cleanup that closes whatever it finds severs the transaction its own
    caller is still inside.

    The ident assertion is load-bearing rather than defensive: if asgiref stopped
    reusing the calling thread, the dispatch would land somewhere with no open
    connection and every remaining assertion would hold for the wrong reason --
    the test would pass while testing nothing.
    """
    _assert_a_close_is_observable()
    # pytest-django runs a ``django_db`` test inside an atomic block, so the
    # connection this thread holds is the real thing this cleanup must not
    # touch -- open, and with a transaction on it.
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
    # ``close()`` inside an atomic block deliberately does *not* blank
    # ``connection.connection`` -- it flags the wrapper and lets the next query
    # be the one that raises. So reading that attribute would show nothing;
    # read the flag, then prove the consequence by using the connection.
    assert connection.closed_in_transaction is False, (
        "the dispatch closed a connection it did not open -- the caller's own, "
        "with a transaction still on it"
    )
    # The consequence rather than only the flag: on a connection closed inside
    # its transaction the *next* query is what raises. Filtered to nothing, so
    # what it asserts is that the query ran at all.
    assert Widget.objects.filter(name="no-such-widget").exists() is False


# --- the back half of a call is overridable ----------------------------------
#
# Everything after the dispatch returns -- pagination, rendering, the resolved-
# data pool, the byte bound -- is a seam as argument intake is, so a project
# changing one of them need not replace ``call_tool`` wholesale.


def test_the_run_context_double_carries_every_field_the_package_reads():
    """Pin the stand-in against the real dataclass, in both directions.

    A double grown field-by-field as tests needed them is how a helper ends up
    describing a context no run produces -- and the failure mode is silent, since
    every test using it agrees with it. Reading a field the real
    ``RunContext`` does not have would pass here and ``AttributeError`` in
    production; carrying one fewer than the package reads would do the reverse.
    """
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
            # A deliberately different window, so the assertion below cannot
            # also be satisfied by the default shaper having run.
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
    """The kernel's ``present`` is the opt-out; ``projection=None`` is not.

    ``present_for_audience`` reads ``projection=None`` as *derive one from the
    spec*, so a project wanting the unprojected payload -- a chaining pipeline
    that needs the handles the next step reads by -- has to present with
    ``present`` instead. That is reachable only because the render step is a
    seam.
    """

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

    # If this ever stops holding, the override has stopped being an opt-out of
    # anything and the test is passing for the wrong reason.
    assert projected != raw
    assert set(raw[0]) - set(projected[0])


@pytest.mark.django_db(transaction=True)
async def test_render_output_is_handed_the_page_s_rows_and_the_envelope_follows():
    """A paged tool's ``render_output`` presents the page, never the whole set.

    Its ``result.value`` is the rows of the page asked for, and what it returns
    becomes the envelope's ``items``, with the envelope added after it returns.
    """
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
    # Cumulative for the run, not attributable to this call -- present so a long
    # autonomous run can be read back against where its budget stood.
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
    # The shape an upsert has in practice: a small DTO carrying the row *and*
    # the flag, which is exactly what an ``output_selector_spec`` re-fetch then
    # replaces in ``DispatchResult.value``.
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
    """created-vs-updated is unanswerable from ``value`` once a re-fetch ran.

    The docstring on ``render_output`` names ``service_result`` as there for an
    override that wants it. With an ``output_selector_spec`` present, ``value``
    is the re-fetched row and the service's own return -- the flags carrier --
    is reachable nowhere else.
    """
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
    """An override taking the per-call request, view and extras raises at the call.

    Those three are the call's seeds and context now, which the kernel hands
    the presenter itself, so an override still declaring them is not handed
    them: ``TypeError`` on the first tool call, naming them, rather than a
    render quietly built from ``None``.
    """
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
    """A row's ``id`` and ``name``, narrowed by a ``fields`` selection it parses.

    Refuses an unknown name in its own words, with no full stop, so a test
    through it shows the retry carries whatever the presenter says.
    """

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
    """A presenter that refuses to render, whatever the call shaped.

    For the cases about *who* shaped the render, where the presenter has to
    fail with nothing supplied as well.
    """

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
    """The reported case, through a real run: ``items`` names the documented
    page's key, and the presenter reads it against one row. The model is told
    which argument, why, and what it applies to, and its second call gets the
    page."""
    user = await User.objects.acreate(username="alice")
    first = await Widget.objects.acreate(owner=user, name="a")
    second = await Widget.objects.acreate(owner=user, name="b")
    selections = iter(["items", "id, name"])

    def model(messages: list[Any], info: Any) -> ModelResponse:
        if any(isinstance(part, ToolReturnPart) for part in messages[-1].parts):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart("list_widgets", {"fields": next(selections)})])

    toolset = SpecToolset({"list_widgets": _paged_spec(_SelectingPresenter())})
    result = await Agent(FunctionModel(model), deps_type=AgentDeps, toolsets=[toolset]).run(
        "go", deps=AgentDeps(user=user)
    )

    retries = [
        part
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, RetryPromptPart)
    ]
    assert [retry.content for retry in retries] == [
        "`fields` was rejected while rendering the result: `items` field is not found. "
        + _SCOPE_DESCRIPTION
    ]
    page = _tool_return(result).content
    assert page["items"] == [{"id": first.pk, "name": "a"}, {"id": second.pk, "name": "b"}]
    assert page["hasNext"] is False


@pytest.mark.django_db
def test_an_unknown_row_field_is_a_retry_not_a_dead_run():
    """Not specific to the envelope: any field the presenter cannot find. A
    valid selection still shapes the rows."""
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
def test_render_error_with_no_query_param_supplied_still_raises():
    """Nothing the model sent shaped this render, so nothing it could send would
    change it: a retry would spend the budget on a server bug and hide it."""
    user = User.objects.create(username="u")
    Widget.objects.create(owner=user, name="a")
    refusal = ServiceValidationError("`bogus` field is not found")

    with pytest.raises(ServiceValidationError, match="`bogus` field is not found"):
        _dispatch(_paged_spec(_RejectingPresenter(refusal)), user, {})


@pytest.mark.django_db
def test_render_error_from_a_seeded_default_still_raises():
    """A default is seeded when the model omits the argument, so after the pop it
    looks exactly like a value the model sent. It is configuration, and a bad one
    has to stay loud. The selecting presenter, so the default is the only thing
    wrong."""
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
    """The consumer's map runs first on this path as on the dispatch path, and its
    handler's value is the tool's result as-is -- whether or not the model shaped
    the render, because the map is the consumer's decision, not the default's."""
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
    """The ``try`` holds the call to the seam, not the default behind it, so a
    consumer rendering its own way gets the same containment."""

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
    """The seat a presenter refusing a shape raises is the kernel's
    ``InvalidArguments``, and the render arm reads it as the caller's to fix."""
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
    """The presenter does not say which one it refused, so singling one out
    would be a guess presented as a fact. Declaration order, so the sentence
    reads the same on every call."""
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
    """Some refusals carry a full stop and some do not; the retry reads right
    either way."""
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
    """A list tool and a shape both present is not enough: declared only on a
    retrieve tool, the shape never meets a page, and the block adds a line only
    when some tool can act on it."""
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
    """A service's ``ServiceValidationError`` carries its detail un-coerced, in
    any of these shapes; the model gets one ``path: message`` line per message,
    with the non-field key not mistaken for an argument's name."""
    with pytest.raises(ModelRetry) as retry:
        _dispatch(_raises(ServiceValidationError(detail)), object(), {})

    assert retry.value.message == expected


@pytest.mark.parametrize("key", ["non_field_errors", "__all__"], ids=["drf-key", "django-key"])
def test_a_message_about_the_whole_input_is_read_without_its_key(key):
    """A service re-raising a model's ``message_dict`` carries its form-wide
    messages under Django's ``__all__``, and the kernel's under
    ``non_field_errors``: either is the line on its own, never a path naming an
    argument the model could not find."""
    detail = {key: ["Name and owner must make a unique set."], "name": ["Too long."]}

    with pytest.raises(ModelRetry) as retry:
        _dispatch(_raises(ServiceValidationError(detail)), object(), {})

    assert retry.value.message == "Name and owner must make a unique set.\nname: Too long."
