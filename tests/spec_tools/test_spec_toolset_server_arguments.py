"""``tool_server_arguments``: values the server supplies for a tool's arguments, and names it withholds.

Written against the public signatures only -- ``SpecToolset``, ``SpecCapability``,
``ServerArguments`` and a real ``Agent`` run -- so each test compiles against a
toolset without the keyword and fails there on what it asserts, not on an import.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypedDict

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django_service_specs import (
    DEFAULT_POOL_SEEDS,
    OutputField,
    Parameter,
    Parameters,
    PermissionCheck,
    SelectorKind,
    SelectorSpec,
    ServerArguments,
    ServiceValidationError,
    SpecRegistry,
)
from pydantic_ai import Agent

from django_spec_agents import AgentDeps
from django_spec_agents.spec_tools import SpecCapability, SpecToolset
from django_spec_agents.testing import tool_calling_model
from tests.spec_tools.kernel_specs import RowPresenter, allow
from tests.spec_tools.test_spec_toolset import (
    _call,
    _input_schema_of,
    _paged_spec,
    _rows,
    _SelectingPresenter,
    _tool_return,
    ctx_for,
)
from tests.spec_tools.testapp.models import Widget


class ArgumentsSeen(PermissionCheck):
    """Admits every call, and records the arguments it was asked with.

    A check declaring ``arguments`` is handed the call's shape-checked
    arguments, so what it records is what every reader of the call is handed:
    a server value the binding put there, and never the model's in its place.
    """

    seen: dict[str, Any] | None = None

    def has_permission(self, principal: Any, spec: Any, *, arguments: Any = None) -> bool:
        ArgumentsSeen.seen = dict(arguments or {})
        return True


@pytest.fixture(autouse=True)
def _forget_the_last_call():
    ArgumentsSeen.seen = None


def _string(name: str, **kwargs: Any) -> Parameter:
    return Parameter(name, "string", **kwargs)


def _names_spec(
    selector: Any,
    *reads: Parameter,
    shapes: tuple[Parameter, ...] = (),
    presenter: RowPresenter | None = None,
) -> SelectorSpec:
    """A list of ``name`` rows, reading ``reads``, guarded by ``ArgumentsSeen``.

    The kernel reflects nothing off a callable, so each argument the selector
    takes from the model is one of ``reads``.
    """
    return SelectorSpec(
        kind=SelectorKind.LIST,
        selector=selector,
        reads=Parameters.of(*reads),
        shapes=Parameters.of(*shapes),
        presenter=presenter or RowPresenter(OutputField("name", "string")),
        permissions=[ArgumentsSeen()],
    )


def vendors_in_scope(*, project_pk: str | None = None, draw_request_pk: str | None = None):
    """The vendors a document may be matched to: its project's, or its draw request's."""
    return [{"name": f"project={project_pk} draw={draw_request_pk}"}]


def vendors_of_project(*, project_pk: str):
    """The vendors of one project."""
    return [{"name": f"project={project_pk}"}]


def any_vendors():
    """Every vendor."""
    return [{"name": "any"}]


def _scoped_spec(selector: Any = vendors_in_scope, *reads: Parameter) -> SelectorSpec:
    return _names_spec(selector, *(reads or (_string("project_pk"), _string("draw_request_pk"))))


def _required_spec() -> SelectorSpec:
    return _scoped_spec(vendors_of_project, _string("project_pk", required=True))


def _registry(**specs: SelectorSpec) -> SpecRegistry:
    registry = SpecRegistry()
    for name, spec in specs.items():
        registry.register(name, spec)
    return registry


_SCOPED = ServerArguments(values={"project_pk": "7"}, withheld=frozenset({"draw_request_pk"}))


# --- construction -------------------------------------------------------------


def test_a_tool_name_the_toolset_does_not_expose_is_refused_naming_it():
    """As every per-tool mapping is: a typo would otherwise bind nothing, silently."""
    with pytest.raises(ValueError, match=r"tool_server_arguments references unknown tool 'nope'"):
        SpecToolset(
            _registry(vendors=_scoped_spec()), tool_server_arguments={"nope": ServerArguments()}
        )


def test_a_name_the_spec_does_not_declare_is_refused_naming_the_tool():
    with pytest.raises(ImproperlyConfigured) as raised:
        SpecToolset(
            _registry(vendors=_scoped_spec()),
            tool_server_arguments={"vendors": ServerArguments(values={"tenant_pk": "1"})},
        )

    assert str(raised.value).startswith("SpecToolset tool 'vendors': ")
    assert "tenant_pk" in str(raised.value)


def test_a_bound_value_its_parameter_refuses_fails_at_construction():
    """A server value failing its own declaration is the server's bug, so it fails
    the mount rather than every call that reaches it."""
    spec = _scoped_spec(vendors_in_scope, Parameter("project_pk", "integer"))

    with pytest.raises(ImproperlyConfigured) as raised:
        SpecToolset(
            _registry(vendors=spec),
            tool_server_arguments={"vendors": ServerArguments(values={"project_pk": "seven"})},
        )

    assert str(raised.value).startswith("SpecToolset tool 'vendors': ")


def test_a_withheld_name_no_call_could_do_without_is_refused():
    """Withheld, the read is absent, and the selector declares it with no default."""
    with pytest.raises(ImproperlyConfigured) as raised:
        SpecToolset(
            _registry(vendors=_required_spec()),
            tool_server_arguments={"vendors": ServerArguments(withheld=frozenset({"project_pk"}))},
        )

    assert str(raised.value).startswith("SpecToolset tool 'vendors': ")
    assert "withholds project_pk" in str(raised.value)


def scoped_to_tenant(*, tenant: str):
    """The rows of one tenant."""
    return [{"name": tenant}]


def test_a_name_a_registered_seed_occupies_is_refused():
    """Checked against the toolset's own seeds: the kernel's default set does not
    hold ``tenant``, so a check that did not hear about them would pass this.

    ``tenant`` is a reflected read, as a library compiling its declaration off
    the callable's signature marks one, so the spec declares the name and the
    seed is what refuses it, not an undeclared name.
    """
    seeds = DEFAULT_POOL_SEEDS.extend(tenant=lambda: "acme")

    with pytest.raises(ImproperlyConfigured, match=r"tool 'rows': .*pool seed") as raised:
        SpecToolset(
            {"rows": _names_spec(scoped_to_tenant, _string("tenant", reflected=True))},
            pool_seeds=seeds,
            tool_server_arguments={"rows": ServerArguments(values={"tenant": "other"})},
        )

    assert "a reflected read which a registered pool seed fills" in str(raised.value)
    assert "does not declare" not in str(raised.value)


# --- the advertised schema ----------------------------------------------------


def test_bound_and_withheld_names_leave_the_schema_and_its_required():
    """Through the registry, where the reads reach the schema as the compiled
    spec's parameters, and a list tool's own arguments are added after: the
    subtraction has to come last, or a required read is written back."""
    registry = _registry(
        vendors=_scoped_spec(
            vendors_in_scope, _string("project_pk", required=True), _string("draw_request_pk")
        )
    )

    unbound = _input_schema_of(SpecToolset(registry), "vendors")
    bound = _input_schema_of(
        SpecToolset(registry, tool_server_arguments={"vendors": _SCOPED}), "vendors"
    )

    assert {"project_pk", "draw_request_pk"} <= set(unbound["properties"])
    assert unbound["required"] == ["project_pk"]
    assert not {"project_pk", "draw_request_pk"} & set(bound["properties"])
    assert "required" not in bound
    # Everything else is as it was.
    assert {k: v for k, v in unbound["properties"].items() if not k.endswith("_pk")} == bound[
        "properties"
    ]


def entry_on_page(*, page_pk: str, book: str, note: str = ""):
    """One entry, by its book and page."""
    return {"name": f"{book}{page_pk}{note}"}


def test_a_tool_with_no_channels_loses_the_bound_name_too():
    """A retrieve tool gains no page arguments, so its schema is the spec's own,
    which has to lose a bound name as a list tool's does."""
    spec = SelectorSpec(
        kind=SelectorKind.RETRIEVE,
        selector=entry_on_page,
        reads=Parameters.of(
            _string("page_pk", required=True),
            _string("book", required=True),
            _string("note", default=""),
        ),
        presenter=RowPresenter(OutputField("name", "string")),
        permissions=[ArgumentsSeen()],
    )
    toolset = SpecToolset(
        {"entry": spec},
        tool_server_arguments={"entry": ServerArguments(values={"page_pk": "3"})},
    )

    schema = _input_schema_of(toolset, "entry")
    assert set(schema["properties"]) == {"book", "note"}
    # What the binding does not answer for is still required.
    assert schema["required"] == ["book"]


def fields_free(*, user: Any):
    """Every vendor, shaped by the ``fields`` the call sends."""
    return [{"name": "v"}]


def _shaped_spec(fields: Parameter, seen: dict[str, Any]) -> SelectorSpec:
    """``fields_free`` with ``fields`` as its one shape, whose render records the call's shapes."""
    return _names_spec(
        fields_free,
        shapes=(fields,),
        presenter=RowPresenter(OutputField("name", "string"), refuse=seen.update),
    )


async def test_a_bound_shape_is_not_offered_in_the_instructions():
    """The read-shaping line names every shape a tool offers; a bound one is not
    offered, so it is not named."""
    registry = _registry(vendors=_shaped_spec(_string("fields"), {}))
    unbound = SpecToolset(registry)
    bound = SpecToolset(
        registry, tool_server_arguments={"vendors": ServerArguments(values={"fields": "name"})}
    )

    assert "`fields`" in (await unbound.get_instructions(ctx_for()) or "")
    assert "fields" not in (await bound.get_instructions(ctx_for()) or "")


# --- each call ----------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_a_binding_fills_a_required_read_the_model_never_sends():
    """The binding is applied before the call is checked for what it requires, so
    a required read the server supplies is never asked of the model. Applied any
    later, the model's empty call is answered with a missing-argument retry."""
    user = await User.objects.acreate(username="u")
    toolset = SpecToolset(
        _registry(vendors=_required_spec()),
        tool_server_arguments={"vendors": ServerArguments(values={"project_pk": "7"})},
    )
    agent = Agent(tool_calling_model("vendors", {}), deps_type=AgentDeps, toolsets=[toolset])

    result = await agent.run("go", deps=AgentDeps(user=user))

    assert _rows(_tool_return(result).content) == [{"name": "project=7"}]
    assert ArgumentsSeen.seen == {"project_pk": "7"}


@pytest.mark.django_db(transaction=True)
async def test_the_bound_value_outranks_the_one_the_model_sends(caplog):
    """Applied before the call is dispatched, so the bound value is the argument
    the selector is handed, and the one a permission check is asked with.
    Applied after, the model's value would already be the call's argument under
    that name."""
    user = await User.objects.acreate(username="u")
    toolset = SpecToolset(
        _registry(vendors=_required_spec()),
        tool_server_arguments={"vendors": ServerArguments(values={"project_pk": "7"})},
    )
    agent = Agent(
        tool_calling_model("vendors", {"project_pk": "99"}), deps_type=AgentDeps, toolsets=[toolset]
    )

    with caplog.at_level(logging.WARNING, logger="django_spec_agents.spec_tools"):
        result = await agent.run("go", deps=AgentDeps(user=user))

    assert _rows(_tool_return(result).content) == [{"name": "project=7"}]
    assert ArgumentsSeen.seen == {"project_pk": "7"}
    (record,) = [r for r in caplog.records if "dropped caller-supplied" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert "'vendors'" in record.getMessage()
    assert "project_pk" in record.getMessage()
    # The name, never the value: a binding is often an identifier the run is
    # scoped by, and a model's attempt at it is no business of the log's.
    assert "99" not in record.getMessage()
    assert "99" not in caplog.text


@pytest.mark.django_db(transaction=True)
async def test_a_withheld_name_takes_nothing_the_model_sends(caplog):
    """``"null"`` for an identifier the model was never shown is dropped before
    anything reads it, so it neither reaches the selector nor refuses the call."""
    user = await User.objects.acreate(username="u")
    toolset = SpecToolset(
        _registry(vendors=_scoped_spec()), tool_server_arguments={"vendors": _SCOPED}
    )

    with caplog.at_level(logging.WARNING, logger="django_spec_agents.spec_tools"):
        result = await _call(
            toolset, "vendors", user, {"project_pk": "99", "draw_request_pk": "null"}
        )

    assert _rows(result) == [{"name": "project=7 draw=None"}]
    assert ArgumentsSeen.seen == {"project_pk": "7"}
    # Worded for both kinds of name: a withheld one is not a value the server
    # supplies, only one the model may not choose.
    assert (
        "dropped caller-supplied argument(s) draw_request_pk, project_pk, which "
        "tool_server_arguments keeps from the model"
    ) in caplog.text


@pytest.mark.django_db(transaction=True)
async def test_nothing_is_logged_when_the_model_sends_no_server_name(caplog):
    user = await User.objects.acreate(username="u")
    toolset = SpecToolset(
        _registry(vendors=_scoped_spec()), tool_server_arguments={"vendors": _SCOPED}
    )

    with caplog.at_level(logging.WARNING, logger="django_spec_agents.spec_tools"):
        await _call(toolset, "vendors", user, {})

    assert "dropped caller-supplied" not in caplog.text


@pytest.mark.django_db(transaction=True)
async def test_a_bound_shape_reaches_the_render():
    """A bound shape reaches the presenter on the call's shapes, as a shape the
    model sends does, with the server's value over the model's."""
    user = await User.objects.acreate(username="u")
    seen: dict[str, Any] = {}
    toolset = SpecToolset(
        _registry(vendors=_shaped_spec(_string("fields"), seen)),
        tool_server_arguments={"vendors": ServerArguments(values={"fields": "name"})},
    )

    await _call(toolset, "vendors", user, {"fields": "secret"})

    assert seen == {"fields": "name"}


@pytest.mark.django_db(transaction=True)
async def test_a_withheld_read_falls_back_to_its_declared_default():
    """Withholding means the caller does not choose, not null: the read's declared
    default answers, whatever the model sent."""
    user = await User.objects.acreate(username="u")
    seen: dict[str, Any] = {}
    toolset = SpecToolset(
        _registry(vendors=_shaped_spec(_string("fields", default="name"), seen)),
        tool_server_arguments={"vendors": ServerArguments(withheld=frozenset({"fields"}))},
    )

    await _call(toolset, "vendors", user, {"fields": "secret"})

    assert seen == {"fields": "name"}


@pytest.mark.django_db(transaction=True)
async def test_a_tool_with_no_binding_is_untouched():
    """A binding is per tool: a sibling's call keeps what the model sent."""
    user = await User.objects.acreate(username="u")
    toolset = SpecToolset(
        _registry(vendors=_scoped_spec(), other=_scoped_spec()),
        tool_server_arguments={"vendors": _SCOPED},
    )

    result = await _call(toolset, "other", user, {"project_pk": "99"})

    assert _rows(result) == [{"name": "project=99 draw=None"}]
    assert "project_pk" in _input_schema_of(toolset, "other")["properties"]


@pytest.mark.django_db(transaction=True)
async def test_a_bound_selection_the_render_rejects_is_the_server_s_error_not_a_retry():
    """A bound read-shaping value is the server's, so a render that rejects it is a
    configuration bug that stays loud, as a rejected declared default does. A
    retry would name an argument the model was never offered and cannot change,
    and on a small budget end the run."""
    user = await User.objects.acreate(username="u")
    await Widget.objects.acreate(owner=user, name="a")
    toolset = SpecToolset(
        _registry(widgets=_paged_spec(_SelectingPresenter())),
        tool_server_arguments={"widgets": ServerArguments(values={"fields": "name, bogus"})},
    )

    with pytest.raises(ServiceValidationError, match="`bogus` field is not found"):
        await _call(toolset, "widgets", user, {})


# --- a consumer scoping each run ----------------------------------------------
#
# A document-enrichment run is scoped to the document's project, or else its draw
# request. One rule decides which tools the run scopes: a tool counts as scoped
# only if the run binds one of its scope names; its optional siblings are then
# the server's as well, and a required one the run does not bind stays the
# model's to send. That rule is one small function from the registry and the
# run's scope to the bindings, so the toolset is not subclassed for it.
#
# The function is the registry page's own example, run as written, so the page
# cannot drift from what these tests hold.

_DOCS_PAGE = (
    Path(__file__).resolve().parent.parent.parent / "docs" / "spec-tools" / "spec-registry.md"
)


def _docs_recipe(registry: SpecRegistry, scope: Mapping[str, str]) -> dict[str, Any]:
    """Run the registry page's per-run scope example over ``registry`` and ``scope``.

    Returns the names the block defined: ``scope_bindings`` and the ``toolset``
    it builds.
    """
    blocks = re.findall(r"```python\n(.*?)```", _DOCS_PAGE.read_text(), flags=re.DOTALL)
    (block,) = [block for block in blocks if "def scope_bindings" in block]
    namespace: dict[str, Any] = {
        "ServerArguments": ServerArguments,
        "SpecToolset": SpecToolset,
        "registry": registry,
        "scope": scope,
    }
    exec(block, namespace)
    return namespace


@pytest.mark.django_db(transaction=True)
async def test_a_run_scoped_to_a_project_binds_it_and_withholds_the_sibling():
    user = await User.objects.acreate(username="u")
    registry = _registry(
        list_document_vendors=_scoped_spec(),
        list_vendors=_names_spec(any_vendors),
    )
    recipe = _docs_recipe(registry, {"project_pk": "7"})
    toolset = recipe["toolset"]
    # The model reaches for the scope anyway -- a guessed project and a
    # placeholder for the draw request it was never shown.
    agent = Agent(
        tool_calling_model(
            "list_document_vendors", {"project_pk": "99", "draw_request_pk": "null"}
        ),
        deps_type=AgentDeps,
        toolsets=[toolset],
    )

    result = await agent.run("enrich", deps=AgentDeps(user=user))

    assert _rows(_tool_return(result).content) == [{"name": "project=7 draw=None"}]
    # Every reader of the call sees the one value: a permission check reading
    # the call's arguments included.
    assert ArgumentsSeen.seen == {"project_pk": "7"}
    schema = _input_schema_of(toolset, "list_document_vendors")
    assert not {"project_pk", "draw_request_pk"} & set(schema["properties"])
    # A tool reading no scope name has no binding.
    assert "list_vendors" not in recipe["scope_bindings"](registry, {"project_pk": "7"})


@pytest.mark.django_db(transaction=True)
async def test_a_tool_reading_only_scope_names_the_run_does_not_bind_keeps_them():
    """A tool reading only ``draw_request_pk``, in a run scoped to a project, is
    not scoped by the run: it has no binding, so the model still sends its draw
    request and the selector receives it. Holds the recipe's skip of a tool with
    nothing bound; without it the name would be withheld, gone from the schema,
    and every call would reach the selector with no draw request."""
    user = await User.objects.acreate(username="u")
    registry = _registry(
        list_draw_vendors=_scoped_spec(vendors_in_scope, _string("draw_request_pk"))
    )
    recipe = _docs_recipe(registry, {"project_pk": "7"})
    toolset = recipe["toolset"]

    result = await _call(toolset, "list_draw_vendors", user, {"draw_request_pk": "3"})

    assert _rows(result) == [{"name": "project=None draw=3"}]
    assert "draw_request_pk" in _input_schema_of(toolset, "list_draw_vendors")["properties"]
    assert recipe["scope_bindings"](registry, {"project_pk": "7"}) == {}


def vendors_of_document(*, document_pk: str, project_pk: str | None = None):
    """The vendors a document may be matched to, within its project if one is given."""
    return [{"name": f"project={project_pk} document={document_pk}"}]


@pytest.mark.django_db(transaction=True)
async def test_a_required_scope_name_the_run_does_not_bind_stays_the_model_s():
    """A scoped tool's sibling it cannot run without is not withheld: withheld, no
    call could supply it, which the toolset refuses when it is built. Holds the
    recipe's ``required`` check; the model is still asked for the document, and
    only the project is the server's."""
    user = await User.objects.acreate(username="u")
    registry = _registry(
        list_document_vendors=_scoped_spec(
            vendors_of_document, _string("project_pk"), _string("document_pk", required=True)
        )
    )
    toolset = _docs_recipe(registry, {"project_pk": "7"})["toolset"]

    result = await _call(
        toolset, "list_document_vendors", user, {"project_pk": "99", "document_pk": "5"}
    )

    assert _rows(result) == [{"name": "project=7 document=5"}]
    schema = _input_schema_of(toolset, "list_document_vendors")
    assert "project_pk" not in schema["properties"]
    assert schema["required"] == ["document_pk"]


def widgets_in_project(*, user: Any, project_pk: int) -> Any:
    """The acting user's widgets in one project, a price ceiling standing in for it."""
    return Widget.objects.filter(owner=user, price__lte=project_pk).order_by("pk")


class _NoKeys(TypedDict):
    """Nothing filled: a provider that only reads."""


@pytest.mark.django_db(transaction=True)
async def test_a_bound_read_reaches_the_selector_and_a_provider_alike():
    """The quickstart's scope: a read the spec declares and the server binds is
    the argument the selector receives, and a ``kwargs=`` provider naming it is
    handed the same value by name, while the model is offered neither.
    Unbound, the read is advertised as declared, and required, since the
    provider's return says it fills nothing; an untyped one may fill any read
    beside it, which is then not required of the model."""
    seen: dict[str, Any] = {}

    def scope_provider(project_pk: int) -> _NoKeys:
        seen["project_pk"] = project_pk
        return _NoKeys()

    user = await User.objects.acreate(username="u")
    await Widget.objects.acreate(name="cheap", price=5, owner=user)
    await Widget.objects.acreate(name="dear", price=15, owner=user)
    spec = SelectorSpec(
        kind=SelectorKind.LIST,
        selector=widgets_in_project,
        reads=Parameters.of(Parameter("project_pk", "integer", required=True)),
        kwargs=scope_provider,
        presenter=RowPresenter(OutputField("name", "string")),
        permissions=allow(),
    )
    toolset = SpecToolset(
        _registry(list_widgets=spec),
        tool_server_arguments={"list_widgets": ServerArguments(values={"project_pk": 7})},
    )

    result = await _call(toolset, "list_widgets", user, {"project_pk": 99})

    assert [row["name"] for row in _rows(result)] == ["cheap"]
    assert seen == {"project_pk": 7}
    assert "project_pk" not in _input_schema_of(toolset, "list_widgets").get("properties", {})
    unbound = _input_schema_of(SpecToolset(_registry(list_widgets=spec)), "list_widgets")
    assert unbound["properties"]["project_pk"]["type"] == "integer"
    assert unbound["required"] == ["project_pk"]


# --- the capability -----------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_the_capability_forwards_the_bindings():
    user = await User.objects.acreate(username="u")
    capability = SpecCapability(
        _registry(vendors=_scoped_spec()), tool_server_arguments={"vendors": _SCOPED}
    )
    toolset = capability.get_toolset()

    result = await _call(toolset, "vendors", user, {"project_pk": "99"})

    assert _rows(result) == [{"name": "project=7 draw=None"}]
    assert "project_pk" not in _input_schema_of(toolset, "vendors")["properties"]


# --- a mount declares no channels ---------------------------------------------


_MOUNT_LEVEL_CHANNELS = [
    ("query_params", [_string("fields")]),
    ("tool_query_params", {"vendors": [_string("fields")]}),
    ("url_kwargs", [_string("tenant_pk")]),
    ("tool_url_kwargs", {"vendors": [_string("tenant_pk")]}),
]


@pytest.mark.parametrize(("knob", "value"), _MOUNT_LEVEL_CHANNELS)
@pytest.mark.parametrize("build", [SpecToolset, SpecCapability], ids=["toolset", "capability"])
def test_a_mount_level_channel_declaration_is_an_unknown_keyword(build, knob, value):
    """A read is the spec's to declare, in its ``reads`` or ``shapes``, and a
    value the model may not choose is ``tool_server_arguments``; no mount
    declares a channel."""
    with pytest.raises(TypeError, match=rf"unexpected keyword argument '{knob}'"):
        build({"vendors": _names_spec(any_vendors)}, **{knob: value})


@pytest.mark.django_db(transaction=True)
async def test_the_bindings_are_read_once_when_the_toolset_is_built():
    """A mapping changed after the toolset is built binds nothing new: the check
    ran on what was passed, and an unchecked binding would reach every call. So
    the call still serves the bound project, and the schema still omits it."""
    user = await User.objects.acreate(username="u")
    bindings = {"vendors": _SCOPED}
    toolset = SpecToolset(_registry(vendors=_scoped_spec()), tool_server_arguments=bindings)

    bindings["vendors"] = ServerArguments()
    result = await _call(toolset, "vendors", user, {"project_pk": "99"})

    assert _rows(result) == [{"name": "project=7 draw=None"}]
    assert "project_pk" not in _input_schema_of(toolset, "vendors")["properties"]


@pytest.mark.django_db(transaction=True)
async def test_a_malformed_value_for_a_bound_name_never_refuses_the_call():
    """The model's value is dropped before the shape check reads it, so an integer
    for a string it was never offered costs no retry, under ``REJECT`` too."""
    user = await User.objects.acreate(username="u")
    toolset = SpecToolset(
        {"vendors": _names_spec(vendors_of_project, _string("project_pk"))},
        tool_server_arguments={"vendors": ServerArguments(values={"project_pk": "7"})},
    )

    result = await _call(toolset, "vendors", user, {"project_pk": 12345})

    assert _rows(result) == [{"name": "project=7"}]
