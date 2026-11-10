from __future__ import annotations

import pathlib
import re
import warnings
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from django.db import connection
from django.db.models import Sum
from django.test.utils import CaptureQueriesContext
from django_service_specs import OutputPage, SelectorKind, SelectorSpec
from pydantic_ai.usage import RunUsage

from django_spec_agents import AgentDeps
from django_spec_agents.spec_tools import SpecCapability, SpecToolset
from tests.spec_tools.kernel_specs import allow, widget_presenter
from tests.spec_tools.testapp.models import Widget

_QUICKSTART = pathlib.Path(__file__).resolve().parents[2] / "docs" / "spec-tools" / "quickstart.md"
_ENVELOPE = {"items", "page", "totalPages", "hasNext"}


def list_widgets(user: Any) -> Any:
    """List widgets owned by the acting user."""
    return Widget.objects.filter(owner=user)


def get_widget(user: Any) -> Any:
    """Fetch the acting user's widget."""
    return None


def _list_spec() -> SelectorSpec:
    return SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_widgets,
        presenter=widget_presenter(),
        permissions=allow(),
    )


def _ctx(user: Any, tool_name: str = "list_widgets") -> SimpleNamespace:
    """The run context fields a call reads, ``tool_name`` included as the real one carries it."""
    return SimpleNamespace(
        deps=AgentDeps(user=user),
        run_id="run-1",
        conversation_id="conv-1",
        run_step=1,
        tool_call_id="call-1",
        tool_name=tool_name,
        usage=RunUsage(),
    )


def _call(toolset: SpecToolset, user: Any, args: dict[str, Any], name: str = "list_widgets") -> Any:
    """One call through the toolset's own seams, on this thread, so its queries are visible."""
    return toolset._call_spec(
        toolset.specs[name], user, dict(args), ctx=_ctx(user, name), label=name
    )


def _widgets(user: Any, count: int, *, start: int = 0) -> None:
    for n in range(start, start + count):
        Widget.objects.create(name=f"w{n}", price=n, owner=user)


@pytest.mark.django_db
def test_the_envelope_carries_no_total_by_default():
    user = User.objects.create(username="u")
    _widgets(user, 3)

    result = _call(SpecToolset({"list_widgets": _list_spec()}), user, {"limit": 2})

    assert set(result) == _ENVELOPE


@pytest.mark.django_db
def test_include_total_reports_every_row_the_selector_matched_not_the_page_or_the_table():
    """Against a real queryset count, with rows the selector filters out."""
    user = User.objects.create(username="u")
    other = User.objects.create(username="other")
    _widgets(user, 5)
    _widgets(other, 2, start=5)
    toolset = SpecToolset({"list_widgets": _list_spec()}, include_total=True)

    result = _call(toolset, user, {"limit": 2, "page": 2})

    assert result["total"] == Widget.objects.filter(owner=user).count() == 5
    assert len(result["items"]) == 2
    assert Widget.objects.count() == 7
    assert set(result) == {*_ENVELOPE, "total"}
    assert (result["page"], result["totalPages"], result["hasNext"]) == (2, 3, True)


@pytest.mark.django_db
def test_an_empty_list_reports_a_total_of_zero():
    user = User.objects.create(username="u")
    toolset = SpecToolset({"list_widgets": _list_spec()}, include_total=True)

    result = _call(toolset, user, {})

    assert result == {"items": [], "page": 1, "totalPages": 1, "hasNext": False, "total": 0}


@pytest.mark.django_db
@pytest.mark.parametrize("include_total", [False, True])
def test_the_total_costs_no_query_of_its_own(include_total):
    """One ``COUNT`` and one page ``SELECT``, whichever way the flag is set."""
    user = User.objects.create(username="u")
    _widgets(user, 3)
    toolset = SpecToolset({"list_widgets": _list_spec()}, include_total=include_total)

    with CaptureQueriesContext(connection) as queries:
        _call(toolset, user, {"limit": 2})

    sql = [query["sql"].upper() for query in queries.captured_queries]
    assert len(sql) == 2, sql
    assert sum("COUNT(" in statement for statement in sql) == 1, sql


def _return_schema(toolset: SpecToolset) -> dict[str, Any]:
    schema = toolset._tool_defs["list_widgets"].return_schema
    assert schema is not None
    return schema


def _limit_description(toolset: SpecToolset) -> str:
    return toolset._tool_defs["list_widgets"].parameters_json_schema["properties"]["limit"][
        "description"
    ]


def test_off_the_advertised_envelope_and_limit_are_as_they_were():
    """Byte for byte, so a toolset leaving the flag alone advertises nothing new."""
    toolset = SpecToolset({"list_widgets": _list_spec()})
    schema = _return_schema(toolset)

    assert set(schema["properties"]) == _ENVELOPE
    assert schema["required"] == ["items", "page", "totalPages", "hasNext"]
    assert _limit_description(toolset) == (
        "Maximum number of items per page. Defaults to 100; "
        "the result reports `totalPages` and `hasNext`."
    )


def test_on_the_advertised_envelope_and_limit_name_the_total():
    toolset = SpecToolset({"list_widgets": _list_spec()}, include_total=True)
    schema = _return_schema(toolset)

    assert schema["properties"]["total"]["type"] == "integer"
    assert schema["required"] == ["items", "page", "totalPages", "hasNext", "total"]
    assert _limit_description(toolset) == (
        "Maximum number of items per page. Defaults to 100; "
        "the result reports `total`, `totalPages` and `hasNext`."
    )


def test_a_tool_that_does_not_page_is_advertised_as_before():
    """The flag describes the page envelope, so a retrieve tool's schema is untouched."""
    spec = SelectorSpec(
        kind=SelectorKind.RETRIEVE,
        selector=get_widget,
        presenter=widget_presenter(),
        permissions=allow(),
    )
    on = SpecToolset({"get": spec}, include_total=True)._tool_defs["get"]
    off = SpecToolset({"get": spec})._tool_defs["get"]

    assert on.return_schema == off.return_schema
    assert on.parameters_json_schema == off.parameters_json_schema


async def test_the_capability_forwards_the_flag():
    capability = SpecCapability({"list_widgets": _list_spec()}, include_total=True)
    tools = await capability.get_toolset().get_tools(None)

    assert "total" in tools["list_widgets"].tool_def.return_schema["properties"]


@dataclass(frozen=True)
class _CountingPage(OutputPage):
    def envelope(self, rendered: Any) -> dict[str, Any]:
        return {**super().envelope(rendered), "total": self.total}


@pytest.mark.django_db
def test_per_tool_is_the_flag_off_and_an_override_counting_the_chosen_tools():
    """What every tool advertises agrees with what it is sent."""

    class OnlyWidgetsCounted(SpecToolset):
        def shape_page(self, rows, *, ctx, page, limit, max_page_size):
            shaped = super().shape_page(
                rows, ctx=ctx, page=page, limit=limit, max_page_size=max_page_size
            )
            if ctx.tool_name != "list_widgets":
                return shaped
            return _CountingPage(
                items=shaped.items, page=shaped.page, limit=shaped.limit, total=shaped.total
            )

    user = User.objects.create(username="u")
    _widgets(user, 3)
    toolset = OnlyWidgetsCounted({"list_widgets": _list_spec(), "list_others": _list_spec()})

    counted = _call(toolset, user, {}, "list_widgets")
    plain = _call(toolset, user, {}, "list_others")

    assert counted["total"] == 3
    assert set(plain) == _ENVELOPE
    for name, payload in (("list_widgets", counted), ("list_others", plain)):
        tool_def = toolset._tool_defs[name]
        assert tool_def.return_schema is not None
        assert set(tool_def.return_schema["required"]) <= set(payload), name
        assert (
            "`total`" not in tool_def.parameters_json_schema["properties"]["limit"]["description"]
        )


def test_a_list_with_no_presenter_still_advertises_no_return_schema():
    """The flag adds ``total`` to a derived envelope only, never a schema it conjures."""
    spec = SelectorSpec(kind=SelectorKind.LIST, selector=list_widgets, permissions=allow())

    assert SpecToolset({"l": spec}, include_total=True)._tool_defs["l"].return_schema is None


def _documented_recipe() -> dict[str, Any]:
    """The aggregates recipe as the toolset page shows it, executed."""
    blocks = re.findall(r"^```python\n(.*?)^```", _QUICKSTART.read_text(), re.MULTILINE | re.DOTALL)
    (recipe,) = [block for block in blocks if "class AggregatedSpecToolset" in block]
    namespace: dict[str, Any] = {}
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        exec(compile(recipe, str(_QUICKSTART), "exec"), namespace)
    return namespace


@pytest.mark.django_db(transaction=True)
async def test_the_documented_aggregates_recipe_runs_through_call_tool():
    """The page's own code over this suite's widgets; only its ``LIST_AGGREGATES`` is swapped."""
    recipe = _documented_recipe()
    recipe["LIST_AGGREGATES"] = {"list_widgets": {"price_total": Sum("price")}, "list_counted": {}}
    user = await sync_to_async(User.objects.create)(username="u")
    other = await sync_to_async(User.objects.create)(username="other")
    for n, owner in ((1, user), (2, user), (4, user), (8, other)):
        await sync_to_async(Widget.objects.create)(name=f"w{n}", price=n, owner=owner)
    toolset = recipe["AggregatedSpecToolset"](
        {"list_widgets": _list_spec(), "list_others": _list_spec(), "list_counted": _list_spec()}
    )
    tools = await toolset.get_tools(None)

    counted = await toolset.call_tool(
        "list_widgets", {"limit": 1}, _ctx(user, "list_widgets"), tools["list_widgets"]
    )
    plain = await toolset.call_tool(
        "list_others", {"limit": 1}, _ctx(user, "list_others"), tools["list_others"]
    )

    only_total = await toolset.call_tool(
        "list_counted", {"limit": 1}, _ctx(user, "list_counted"), tools["list_counted"]
    )

    assert (counted["total"], counted["price_total"], len(counted["items"])) == (3, 7, 1)
    assert set(plain) == _ENVELOPE
    assert set(only_total) == {*_ENVELOPE, "total"}
    assert only_total["total"] == 3


@pytest.mark.django_db
def test_the_documented_recipe_runs_no_aggregate_for_an_empty_entry():
    """``total`` alone is the count already taken, so an empty entry costs no query."""
    recipe = _documented_recipe()
    recipe["LIST_AGGREGATES"] = {"list_widgets": {}}
    user = User.objects.create(username="u")
    _widgets(user, 3)
    toolset = recipe["AggregatedSpecToolset"]({"list_widgets": _list_spec()})

    with CaptureQueriesContext(connection) as queries:
        result = _call(toolset, user, {"limit": 2})

    assert result["total"] == 3
    assert len(queries.captured_queries) == 2


@pytest.mark.django_db
def test_the_documented_recipe_sums_an_empty_list_to_zero():
    recipe = _documented_recipe()
    recipe["LIST_AGGREGATES"] = {"list_widgets": {"price_total": Sum("price")}}
    user = User.objects.create(username="u")
    toolset = recipe["AggregatedSpecToolset"]({"list_widgets": _list_spec()})

    result = _call(toolset, user, {})

    assert (result["total"], result["price_total"]) == (0, 0)
