"""``conventions=``: each line of model-facing wording changed on its own.

The fixture turns on every line the toolset can say, and ``_surfaces`` collects
everything a model can read from it -- the instructions, every tool definition
and the three retries -- so a test can say both where a field lands and that
nothing else moved.
"""

from __future__ import annotations

import json
from dataclasses import fields
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django_service_specs import DEFAULT_PAGE_SIZE, Affordance, SelectorKind
from pydantic_ai import Agent, ModelRetry
from rest_framework.permissions import AllowAny
from rest_framework_services import QueryParam, SelectorSpec, ServiceSpec, UrlKwarg

from django_spec_agents import AgentDeps
from django_spec_agents.spec_tools import AgentConventions, SpecCapability, SpecToolset
from django_spec_agents.testing import instruction_capturing_model
from tests.spec_tools.test_spec_toolset import reading
from tests.spec_tools.testapp.serializers import WidgetInputSerializer
from tests.spec_tools.wording_specs import (
    HandledWidget,
    Picky,
    approve,
    one,
    run_context,
    selector_specs,
)

_DEFAULTS = AgentConventions()
_FIELDS = [field.name for field in fields(AgentConventions)]
_BLOCK = ["base", "pagination", "ordering", "handles", "read_shaping", "read_shaping_on_pages"]
_ALLOWED_WITH_INSTRUCTIONS = [
    "unavailable_heading",
    "handle_field_description",
    "query_param_on_pages",
    "missing_arguments",
]
_PLACEHOLDERS = {"pagination": ("page_size",), "ordering": ("names",)} | {
    name: ("names",) for name in ("read_shaping", "missing_arguments")
}
_UNAVAILABLE_ITEM = "\n  - `approve`: The books are closed."


def _make(**data: Any) -> dict[str, Any]:
    """Make a widget."""
    return data


def _conditioned(when: Any, **kwargs: Any) -> ServiceSpec:
    return ServiceSpec(
        service=approve,
        atomic=False,
        affordances=[Affordance(code="books_closed", reason="The books are closed.", when=when)],
        permission_classes=[AllowAny],
        **kwargs,
    )


def _every_line() -> dict[str, Any]:
    """A spec map that turns on every line of the block and the unavailable list."""
    return {
        **selector_specs(),
        "scoped_widget": SelectorSpec(
            kind=SelectorKind.RETRIEVE,
            selector=one,
            output_serializer=Picky,
            permission_classes=[AllowAny],
        ),
        "make_widget": ServiceSpec(
            service=_make,
            input_serializer=WidgetInputSerializer,
            atomic=False,
            permission_classes=[AllowAny],
        ),
        "approve": _conditioned(lambda: False),
    }


def _toolset(**kwargs: Any) -> SpecToolset:
    return SpecToolset(
        reading(
            _every_line(),
            {
                "list_widgets": [QueryParam("fields")],
                "scoped_widget": [UrlKwarg("project_pk", required=True)],
            },
        ),
        **kwargs,
    )


async def _retry(toolset: SpecToolset, tools: dict[str, Any], name: str, args: dict) -> str:
    with pytest.raises(ModelRetry) as raised:
        await toolset.call_tool(name, args, run_context(), tools[name])
    return raised.value.message


async def _surfaces(**kwargs: Any) -> dict[str, Any]:
    """Everything the model can read from ``_toolset(**kwargs)``, by where it lands."""
    toolset = _toolset(**kwargs)
    tools = await toolset.get_tools(run_context())
    surfaces: dict[str, Any] = {"instructions": await toolset.get_instructions(run_context())}
    for name, tool in tools.items():
        surfaces[f"params:{name}"] = json.dumps(
            tool.tool_def.parameters_json_schema, ensure_ascii=False
        )
        surfaces[f"return:{name}"] = json.dumps(tool.tool_def.return_schema, ensure_ascii=False)
        surfaces[f"description:{name}"] = tool.tool_def.description
    surfaces["render_retry"] = await _retry(toolset, tools, "list_widgets", {"fields": "{items}"})
    surfaces["missing_retry"] = await _retry(toolset, tools, "get_widget", {})
    surfaces["url_kwarg_retry"] = await _retry(toolset, tools, "scoped_widget", {"pk": 1})
    surfaces["serializer_retry"] = await _retry(toolset, tools, "make_widget", {"price": 1})
    return surfaces


_LANDS = {name: {"instructions"} for name in [*_BLOCK, "unavailable_heading"]} | {
    "handle_field_description": {"return:get_widget"},
    "query_param_on_pages": {"params:list_widgets", "render_retry"},
    "missing_arguments": {"missing_retry", "url_kwarg_retry"},
}

_VALUES = {
    ("pagination", "instructions"): {"page_size": DEFAULT_PAGE_SIZE},
    ("ordering", "instructions"): {"names": "`ordering`"},
    ("read_shaping", "instructions"): {"names": "`fields`"},
    ("missing_arguments", "missing_retry"): {"names": "`pk`"},
    ("missing_arguments", "url_kwarg_retry"): {"names": "`project_pk`"},
}


def _changed(name: str) -> str:
    """Different text for ``name``, using every placeholder it accepts."""
    return f"Changed {name}." + "".join(f" {{{p}}}" for p in _PLACEHOLDERS.get(name, ()))


def _assert_only(changed: dict[str, Any], default: dict[str, Any], where: dict[str, Any]) -> None:
    """``changed`` is ``default`` with exactly the surfaces in ``where`` rewritten."""
    assert set(changed) == set(default)
    for surface, value in default.items():
        assert changed[surface] == where.get(surface, value), surface


def test_the_fixture_reaches_every_field():
    """Otherwise a field missing from ``_LANDS`` would be tested nowhere."""
    assert set(_LANDS) == set(_FIELDS)


@pytest.mark.parametrize("name", _FIELDS)
async def test_each_field_changes_its_own_line_and_nothing_else(name):
    default = await _surfaces()
    changed = await _surfaces(conventions=AgentConventions(**{name: _changed(name)}))

    where = {}
    for surface in _LANDS[name]:
        values = _VALUES.get((name, surface), {})
        old = getattr(_DEFAULTS, name).format(**values)
        assert default[surface].count(old) == 1, surface
        where[surface] = default[surface].replace(old, _changed(name).format(**values))
    _assert_only(changed, default, where)


async def test_a_field_changed_under_instructions_lands_in_the_same_places():
    """The four that ``instructions=`` leaves in force, each where it lands without it."""
    house = {"instructions": "House rules."}
    default = await _surfaces(**house)
    for name in _ALLOWED_WITH_INSTRUCTIONS:
        changed = await _surfaces(conventions=AgentConventions(**{name: _changed(name)}), **house)

        where = {}
        for surface in _LANDS[name]:
            values = _VALUES.get((name, surface), {})
            old = getattr(_DEFAULTS, name).format(**values)
            where[surface] = default[surface].replace(old, _changed(name).format(**values))
        _assert_only(changed, default, where)
    # The heading is the block line an override does not replace, so it is there.
    assert default["instructions"].startswith(f"House rules.\n{_DEFAULTS.unavailable_heading}")


@pytest.mark.parametrize(
    ("name", "gone"),
    [
        ("base", f"{_DEFAULTS.base}\n"),
        ("pagination", f"\n{_DEFAULTS.pagination.format(page_size=DEFAULT_PAGE_SIZE)}"),
        ("ordering", f"\n{_DEFAULTS.ordering.format(names='`ordering`')}"),
        ("handles", f"\n{_DEFAULTS.handles}"),
        # The continuation goes with the line it continues.
        (
            "read_shaping",
            f"\n{_DEFAULTS.read_shaping.format(names='`fields`')} {_DEFAULTS.read_shaping_on_pages}",
        ),
        ("read_shaping_on_pages", f" {_DEFAULTS.read_shaping_on_pages}"),
        # The heading, and the list it introduces.
        ("unavailable_heading", f"\n{_DEFAULTS.unavailable_heading}{_UNAVAILABLE_ITEM}"),
    ],
    ids=[*_BLOCK, "unavailable_heading"],
)
async def test_none_drops_a_block_line_and_nothing_else(name, gone):
    default = await _surfaces()
    dropped = await _surfaces(conventions=AgentConventions(**{name: None}))

    assert default["instructions"].count(gone) == 1
    _assert_only(dropped, default, {"instructions": default["instructions"].replace(gone, "")})


async def test_an_empty_line_leaves_a_blank_line_where_none_drops_it():
    """What the quickstart warns about: ``""`` is a line with nothing on it."""
    default = await _surfaces()
    blank = await _surfaces(conventions=AgentConventions(handles=""))

    gone = f"\n{_DEFAULTS.handles}\n"
    assert default["instructions"].count(gone) == 1
    _assert_only(blank, default, {"instructions": default["instructions"].replace(gone, "\n\n")})


async def test_none_drops_the_handle_description_from_the_output_schema():
    default = await _surfaces()
    dropped = await _surfaces(conventions=AgentConventions(handle_field_description=None))

    returned = json.loads(default["return:get_widget"])
    del returned["properties"]["id"]["description"]
    _assert_only(dropped, default, {"return:get_widget": json.dumps(returned, ensure_ascii=False)})


async def test_none_drops_the_scope_sentence_from_the_param_and_the_retry():
    default = await _surfaces()
    dropped = await _surfaces(conventions=AgentConventions(query_param_on_pages=None))

    accepted = json.loads(default["params:list_widgets"])
    # The param declares no description of its own, so the sentence was all of it.
    del accepted["properties"]["fields"]["description"]
    _assert_only(
        dropped,
        default,
        {
            "params:list_widgets": json.dumps(accepted, ensure_ascii=False),
            "render_retry": "`fields` was rejected while rendering the result: "
            "Unknown field `items`.",
        },
    )


async def test_a_declared_param_description_keeps_its_place_ahead_of_a_changed_sentence():
    toolset = SpecToolset(
        reading(
            {"list_widgets": _every_line()["list_widgets"]},
            {"list_widgets": [QueryParam("fields", description="Which fields.")]},
        ),
        conventions=AgentConventions(query_param_on_pages="Per row."),
    )
    tools = await toolset.get_tools(run_context())

    schema = tools["list_widgets"].tool_def.parameters_json_schema
    assert schema["properties"]["fields"]["description"] == "Which fields. Per row."


async def test_a_doubled_brace_reaches_the_model_as_one():
    """Every field is rendered as a template, placeholders or not, so ``{{`` means ``{``.

    Each field taking no placeholder is set here, because those are the ones a
    site could pass through unrendered and still say something: the base, the
    handle and scope lines and the unavailable heading in the instructions, the
    scope sentence in the list tool's schema and in its render retry, and the
    handle's fallback description.
    """
    surfaces = await _surfaces(
        conventions=AgentConventions(
            base="Tools {{base}}.",
            handles="- Ids look like {{this}}.",
            read_shaping_on_pages="Per row {{scope}}.",
            unavailable_heading="Closed {{now}}:",
            handle_field_description="An id, {{opaque}}.",
            query_param_on_pages="Per item {{row}}.",
            missing_arguments="Send {{{names}}}.",
        )
    )

    instructions = surfaces["instructions"]
    assert instructions.startswith("Tools {base}.\n")
    assert "\n- Ids look like {this}.\n" in instructions
    assert f"{_DEFAULTS.read_shaping.format(names='`fields`')} Per row {{scope}}." in instructions
    assert instructions.endswith(f"\nClosed {{now}}:{_UNAVAILABLE_ITEM}")
    accepted = json.loads(surfaces["params:list_widgets"])
    assert accepted["properties"]["fields"]["description"] == "Per item {row}."
    assert surfaces["render_retry"] == (
        "`fields` was rejected while rendering the result: Unknown field `items`. Per item {row}."
    )
    assert json.loads(surfaces["return:get_widget"])["properties"]["id"]["description"] == (
        "An id, {opaque}."
    )
    assert surfaces["missing_retry"] == "Send {`pk`}."


async def test_an_input_serializer_still_words_its_own_required_fields():
    """``missing_arguments`` is the check made before the serializer, and only that."""
    surfaces = await _surfaces(conventions=AgentConventions(missing_arguments="Need {names}."))

    assert surfaces["missing_retry"] == "Need `pk`."
    assert surfaces["serializer_retry"] == "name: This field is required."


# --- gating survives an override ---------------------------------------------


async def test_an_overridden_line_still_appears_only_when_a_tool_can_act_on_it():
    """No list, no sort, no handle, no read-shaping param: only the base is said."""
    every_line = AgentConventions(
        **{name: _changed(name) for name in _PLACEHOLDERS}
        | {
            "base": "House base.",
            "handles": "Handles.",
            "read_shaping_on_pages": "On pages.",
        }
    )
    toolset = SpecToolset(
        {
            "scoped_widget": _every_line()["scoped_widget"],
            "make_widget": _every_line()["make_widget"],
        },
        conventions=every_line,
    )

    assert await toolset.get_instructions(run_context()) == "House base."


async def test_an_overridden_line_follows_a_tool_a_condition_leaves_out():
    """Re-derived per step, override and all.

    The handle and the read-shaping param are on the conditioned tool only, so
    their lines are there exactly while it is offered. The pagination line cannot
    be shown this way: a ``SelectorSpec`` has no operation conditions, so a list
    tool is never left out by one.
    """
    books = {"open": False}
    toolset = SpecToolset(
        reading(
            {
                "get_widget": _every_line()["scoped_widget"],
                "approve": _conditioned(
                    lambda: books["open"],
                    output_selector_spec=SelectorSpec(
                        kind=SelectorKind.RETRIEVE, output_serializer=HandledWidget
                    ),
                ),
            },
            {"approve": [QueryParam("fields")]},
        ),
        conventions=AgentConventions(
            handles="Handles.", read_shaping="Shape with {names}.", unavailable_heading="Closed:"
        ),
    )

    closed = await toolset.get_instructions(run_context())
    books["open"] = True
    opened = await toolset.get_instructions(run_context())

    assert closed == f"{_DEFAULTS.base}\nClosed:{_UNAVAILABLE_ITEM}"
    assert opened == f"{_DEFAULTS.base}\nHandles.\nShape with `fields`."


async def test_a_block_with_every_line_dropped_is_no_instructions_at_all():
    toolset = SpecToolset(
        {"scoped_widget": _every_line()["scoped_widget"]}, conventions=AgentConventions(base=None)
    )

    assert await toolset.get_instructions(run_context()) is None


@pytest.mark.parametrize("route", ["toolsets", "capability", "capability_from_toolset"])
async def test_an_agent_takes_no_instructions_from_a_toolset_that_says_nothing(route):
    """``None`` from ``get_instructions`` adds nothing to the prompt, by every route.

    Every line of the block dropped and nothing unavailable, so the toolset's own
    answer is ``None``; a real ``Agent`` has to accept that and send the model
    only its own instructions, with no blank line where the block would have been.
    """
    options: dict[str, Any] = {
        "conventions": AgentConventions(**dict.fromkeys([*_BLOCK, "unavailable_heading"])),
    }
    specs = reading(
        {name: spec for name, spec in _every_line().items() if name != "approve"},
        {"list_widgets": [QueryParam("fields")]},
    )
    toolset = SpecToolset(specs, **options)
    assert await toolset.get_instructions(run_context()) is None
    attached: dict[str, Any] = {
        "toolsets": {"toolsets": [toolset]},
        "capability": {"capabilities": [SpecCapability(specs, **options)]},
        "capability_from_toolset": {"capabilities": [SpecCapability.from_toolset(toolset)]},
    }[route]
    captured: dict[str, Any] = {}
    agent = Agent(
        instruction_capturing_model(captured),
        deps_type=AgentDeps,
        instructions="Agent rules.",
        **attached,
    )

    await agent.run("go", deps=AgentDeps(user=None))

    assert captured["instructions"] == "Agent rules."


async def test_with_every_line_dropped_the_unavailable_list_stands_alone():
    """No leading blank line where the block would have been."""
    toolset = SpecToolset(
        {"approve": _conditioned(lambda: False)}, conventions=AgentConventions(base=None)
    )

    assert await toolset.get_instructions(run_context()) == (
        f"{_DEFAULTS.unavailable_heading}{_UNAVAILABLE_ITEM}"
    )


# --- instructions= -----------------------------------------------------------


@pytest.mark.parametrize("name", _BLOCK)
@pytest.mark.parametrize("value", ["Other.", None], ids=["changed", "dropped"])
def test_a_block_line_changed_beside_an_instructions_override_is_refused(name, value):
    """The override replaces the block, so the change would be ignored without a word."""
    with pytest.raises(ImproperlyConfigured) as refused:
        SpecToolset(
            _every_line(),
            instructions="House rules.",
            conventions=AgentConventions(**{name: value}),
        )

    message = str(refused.value)
    assert f"conventions= changes {name}," in message
    assert "instructions=" in message


def test_every_refused_field_is_named_at_once():
    with pytest.raises(ImproperlyConfigured, match=r"changes base, handles,"):
        SpecToolset(
            _every_line(),
            instructions="House rules.",
            conventions=AgentConventions(handles="Handles.", base="Base.", missing_arguments="M."),
        )


def test_the_default_conventions_beside_an_instructions_override_are_fine():
    toolset = SpecToolset(
        _every_line(), instructions="House rules.", conventions=AgentConventions()
    )

    assert toolset.id == "spec-tools"


async def test_a_dropped_heading_drops_the_unavailable_list_after_an_override_too():
    """The list is appended after ``instructions=`` only while it has a heading to go under."""
    kept = SpecToolset(_every_line(), instructions="House rules.")
    dropped = SpecToolset(
        _every_line(),
        instructions="House rules.",
        conventions=AgentConventions(unavailable_heading=None),
    )

    assert await kept.get_instructions(run_context()) == (
        f"House rules.\n{_DEFAULTS.unavailable_heading}{_UNAVAILABLE_ITEM}"
    )
    assert await dropped.get_instructions(run_context()) == "House rules."


def test_the_refusal_reaches_through_the_capability():
    with pytest.raises(ImproperlyConfigured, match=r"conventions= changes pagination,"):
        SpecCapability(
            _every_line(),
            instructions="House rules.",
            conventions=AgentConventions(pagination=None),
        )


# --- through a capability ----------------------------------------------------


@pytest.mark.parametrize("route", ["constructor", "from_toolset"])
async def test_conventions_reach_the_model_through_a_capability(route):
    """django-ag-ui wraps a bare toolset with ``from_toolset``, so that route has to keep them."""
    conventions = AgentConventions(base="House base.", handles=None)
    specs = {"get_widget": _every_line()["get_widget"]}
    if route == "constructor":
        capability = SpecCapability(specs, conventions=conventions)
    else:
        capability = SpecCapability.from_toolset(SpecToolset(specs, conventions=conventions))
    captured: dict[str, Any] = {}
    agent = Agent(
        instruction_capturing_model(captured), deps_type=AgentDeps, capabilities=[capability]
    )

    await agent.run("go", deps=AgentDeps(user=None))

    assert captured["instructions"] == "House base."


async def test_the_quickstart_example_says_what_it_shows():
    """The example under "Changing what the model is told", as the docs write it."""
    conventions = AgentConventions(
        pagination=(
            "- Collections come back one page at a time, as "
            '{{"items": [...], "page": 1, "totalPages": N, "hasNext": true|false}}, '
            "{page_size} items unless you pass `limit`. Ask for the next `page` while "
            "`hasNext` is true."
        ),
        handles=None,
    )
    toolset = SpecToolset(
        {name: _every_line()[name] for name in ("list_widgets", "get_widget")},
        conventions=conventions,
    )

    assert await toolset.get_instructions(run_context()) == (
        f"{_DEFAULTS.base}\n"
        "- Collections come back one page at a time, as "
        '{"items": [...], "page": 1, "totalPages": N, "hasNext": true|false}, '
        f"{DEFAULT_PAGE_SIZE} items unless you pass `limit`. Ask for the next `page` while "
        "`hasNext` is true."
    )
