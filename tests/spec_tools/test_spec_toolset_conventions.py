from __future__ import annotations

import json
from dataclasses import dataclass, fields
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django_service_specs import (
    DEFAULT_PAGE_SIZE,
    Affordance,
    DataclassValidator,
    Parameter,
    Parameters,
    SelectorKind,
    SelectorSpec,
    ServiceSpec,
)
from pydantic_ai import Agent, ModelRetry

from django_spec_agents import AgentDeps
from django_spec_agents.spec_tools import AgentConventions, SpecCapability, SpecToolset
from django_spec_agents.testing import instruction_capturing_model
from tests.spec_tools.kernel_specs import allow, shaping
from tests.spec_tools.wording_specs import (
    approve,
    handled_widget,
    one,
    picky,
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
_PLACEHOLDERS = {
    "pagination": ("envelope", "page_size"),
    "ordering": ("names",),
    "read_shaping_on_pages": ("envelope",),
    "query_param_on_pages": ("envelope",),
} | {name: ("names",) for name in ("read_shaping", "missing_arguments")}
_UNAVAILABLE_ITEM = "\n  - `approve`: The books are closed."
# What ``{envelope}`` renders as, written out: the page's shape in ``pagination``,
# its keys in the two fields beside a read-shaping parameter. ``total`` comes last,
# where the page under ``include_total`` writes it.
_SHAPE = '{"items": [...], "page": 1, "totalPages": N, "hasNext": true|false}'
_SHAPE_WITH_TOTAL = (
    '{"items": [...], "page": 1, "totalPages": N, "hasNext": true|false, "total": T}'
)
_KEYS = "`items`, `page`, `totalPages`, `hasNext`"
_KEYS_WITH_TOTAL = "`items`, `page`, `totalPages`, `hasNext`, `total`"


@dataclass
class _WidgetInput:
    name: str
    price: int


def _make(data: _WidgetInput) -> dict[str, Any]:
    """Make a widget."""
    return data


def _conditioned(when: Any, **kwargs: Any) -> ServiceSpec:
    return ServiceSpec(
        service=approve,
        atomic=False,
        affordances=[Affordance(code="books_closed", reason="The books are closed.", when=when)],
        permissions=allow(),
        **kwargs,
    )


def _every_line() -> dict[str, Any]:
    """A spec map that turns on every line of the block and the unavailable list."""
    return {
        **selector_specs(),
        "scoped_widget": SelectorSpec(
            kind=SelectorKind.RETRIEVE,
            selector=one,
            reads=Parameters.of(
                Parameter("pk", "integer", required=True),
                Parameter("project_pk", "integer", required=True),
            ),
            presenter=picky(),
            permissions=allow(),
        ),
        "make_widget": ServiceSpec(
            service=_make,
            validator=DataclassValidator(_WidgetInput),
            atomic=False,
            permissions=allow(),
        ),
        "approve": _conditioned(lambda: False),
    }


def _toolset(**kwargs: Any) -> SpecToolset:
    return SpecToolset(
        shaping(_every_line(), {"list_widgets": [Parameter("fields", "string")]}),
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
    ("pagination", "instructions"): {"page_size": DEFAULT_PAGE_SIZE, "envelope": _SHAPE},
    ("read_shaping_on_pages", "instructions"): {"envelope": _KEYS},
    ("query_param_on_pages", "params:list_widgets"): {"envelope": _KEYS},
    ("query_param_on_pages", "render_retry"): {"envelope": _KEYS},
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
    assert default["instructions"].startswith(f"House rules.\n{_DEFAULTS.unavailable_heading}")


@pytest.mark.parametrize(
    ("name", "gone"),
    [
        ("base", f"{_DEFAULTS.base}\n"),
        (
            "pagination",
            f"\n{_DEFAULTS.pagination.format(page_size=DEFAULT_PAGE_SIZE, envelope=_SHAPE)}",
        ),
        ("ordering", f"\n{_DEFAULTS.ordering.format(names='`ordering`')}"),
        ("handles", f"\n{_DEFAULTS.handles}"),
        # The continuation goes with the line it continues.
        (
            "read_shaping",
            f"\n{_DEFAULTS.read_shaping.format(names='`fields`')} "
            f"{_DEFAULTS.read_shaping_on_pages}",
        ),
        ("read_shaping_on_pages", f" {_DEFAULTS.read_shaping_on_pages}"),
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


@pytest.mark.parametrize(
    ("include_total", "shape", "keys"),
    [(False, _SHAPE, _KEYS), (True, _SHAPE_WITH_TOTAL, _KEYS_WITH_TOTAL)],
    ids=["off", "include_total"],
)
async def test_the_envelope_placeholder_follows_the_flag(include_total, shape, keys):
    """``{envelope}`` is the page as this toolset writes it, in every field that takes it."""
    surfaces = await _surfaces(
        conventions=AgentConventions(
            pagination="- Pages: {envelope}, {page_size} a page.",
            read_shaping_on_pages="Not on {envelope}.",
            query_param_on_pages="Rows only, not {envelope}.",
        ),
        include_total=include_total,
    )

    instructions = surfaces["instructions"]
    accepted = json.loads(surfaces["params:list_widgets"])
    assert f"\n- Pages: {shape}, {DEFAULT_PAGE_SIZE} a page.\n" in instructions
    assert "without filtering it. Not on " + keys + ".\n" in instructions
    assert accepted["properties"]["fields"]["description"] == f"Rows only, not {keys}."
    assert surfaces["render_retry"].endswith(f"`items`. Rows only, not {keys}.")


@pytest.mark.parametrize("include_total", [False, True], ids=["off", "include_total"])
async def test_text_without_the_envelope_placeholder_renders_as_written(include_total):
    """An override written before the placeholder says exactly what it said, flag or not."""
    surfaces = await _surfaces(
        conventions=AgentConventions(
            pagination='- Pages look like {{"items": [...]}}, {page_size} at a time.',
            read_shaping_on_pages="Per row.",
            query_param_on_pages="Per row, never the page.",
        ),
        include_total=include_total,
    )

    instructions = surfaces["instructions"]
    accepted = json.loads(surfaces["params:list_widgets"])
    assert f'\n- Pages look like {{"items": [...]}}, {DEFAULT_PAGE_SIZE} at a time.\n' in (
        instructions
    )
    assert "without filtering it. Per row.\n" in instructions
    assert accepted["properties"]["fields"]["description"] == "Per row, never the page."
    assert surfaces["render_retry"].endswith("`items`. Per row, never the page.")


async def test_the_scope_names_total_beside_an_instructions_override():
    """``query_param_on_pages`` is in force beside ``instructions=``, and so is the flag."""
    surfaces = await _surfaces(instructions="House rules.", include_total=True)

    accepted = json.loads(surfaces["params:list_widgets"])
    scope = (
        "On a paged result it applies to each item in `items`, never to the page envelope "
        f"({_KEYS_WITH_TOTAL})."
    )
    assert accepted["properties"]["fields"]["description"] == scope
    assert surfaces["render_retry"].endswith(f"`items`. {scope}")


async def test_a_declared_param_description_keeps_its_place_ahead_of_a_changed_sentence():
    toolset = SpecToolset(
        shaping(
            {"list_widgets": _every_line()["list_widgets"]},
            {"list_widgets": [Parameter("fields", "string", help="Which fields.")]},
        ),
        conventions=AgentConventions(query_param_on_pages="Per row."),
    )
    tools = await toolset.get_tools(run_context())

    schema = tools["list_widgets"].tool_def.parameters_json_schema
    assert schema["properties"]["fields"]["description"] == "Which fields. Per row."


async def test_a_doubled_brace_reaches_the_model_as_one():
    """Every field is rendered as a template, placeholders or not, so ``{{`` means ``{``."""
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


async def test_a_validator_still_words_its_own_required_fields():
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
    """Re-derived per step, override and all."""
    books = {"open": False}
    toolset = SpecToolset(
        shaping(
            {
                "get_widget": _every_line()["scoped_widget"],
                "approve": _conditioned(lambda: books["open"], presenter=handled_widget()),
            },
            {"approve": [Parameter("fields", "string")]},
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
    """``None`` from ``get_instructions`` adds nothing to the prompt, by every route."""
    options: dict[str, Any] = {
        "conventions": AgentConventions(**dict.fromkeys([*_BLOCK, "unavailable_heading"])),
    }
    specs = shaping(
        {name: spec for name, spec in _every_line().items() if name != "approve"},
        {"list_widgets": [Parameter("fields", "string")]},
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


@pytest.mark.parametrize(
    ("include_total", "shape"), [(False, _SHAPE), (True, _SHAPE_WITH_TOTAL)], ids=["off", "on"]
)
async def test_the_quickstart_example_says_what_it_shows(include_total, shape):
    """The example under "Changing what the model is told", as the docs write it."""
    conventions = AgentConventions(
        pagination=(
            "- Collections come back one page at a time, as {envelope}, "
            "{page_size} items unless you pass `limit`. Ask for the next `page` while "
            "`hasNext` is true."
        ),
        handles=None,
    )
    toolset = SpecToolset(
        {name: _every_line()[name] for name in ("list_widgets", "get_widget")},
        conventions=conventions,
        include_total=include_total,
    )

    assert await toolset.get_instructions(run_context()) == (
        f"{_DEFAULTS.base}\n"
        f"- Collections come back one page at a time, as {shape}, "
        f"{DEFAULT_PAGE_SIZE} items unless you pass `limit`. Ask for the next `page` while "
        "`hasNext` is true."
    )
