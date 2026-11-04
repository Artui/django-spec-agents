"""The advertised tool definitions, pinned as the toolset hands them to the model.

A deployed agent's model is offered what ``SpecToolset.get_tools`` returns: each
tool's name, its description, its parameters JSON schema and the rest. A change
to any of them is a change to an API whose callers are not in this repository,
so the definitions are checked in at ``tests/spec_tools/tool_schemas.json`` and
this test diffs them.

The dump is what ``get_tools`` returns, read off each ``ToolDefinition``.
Nothing here builds an entry by hand, so the file moves when the toolset's
output does, and only then. It keeps the fields this package sets on a
definition (``name``, ``description``, ``parameters_json_schema``,
``return_schema``, ``metadata``) rather than every field pydantic-ai declares:
pydantic-ai adds fields with defaults in its minor releases, and a lock bump
that adds one changes nothing this package advertises.

The toolset below is built for the dump, over the spec test app's shapes: one
tool per schema feature a definition carries, not one per test spec. Adding a
feature to the definitions earns a tool here.

A schema change regenerates the dump in the same pull request::

    make tool-schemas

which runs this test with ``TOOL_SCHEMAS_WRITE=1`` set, so it writes the file
instead of comparing; the diff of ``tool_schemas.json`` is then the change a
reviewer reads.
"""

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
from typing import Any

import django_filters
from django_service_specs import (
    FieldMarking,
    OutputField,
    Parameter,
    Parameters,
    SelectorKind,
    SelectorSpec,
    ServerArguments,
    ServiceSpec,
    SpecRegistry,
)

from django_spec_agents.spec_tools import SpecToolset
from tests.spec_tools.kernel_specs import RowPresenter, allow, widget_input, widget_presenter
from tests.spec_tools.testapp.models import Widget
from tests.spec_tools.wording_specs import run_context

DUMP = Path(__file__).parent / "tool_schemas.json"
WRITE = os.environ.get("TOOL_SCHEMAS_WRITE") == "1"
FIELDS = ("name", "description", "parameters_json_schema", "return_schema", "metadata")


class _SortedWidgets(django_filters.FilterSet):
    """An ``ordering`` the kernel reads into the spec's reads, as an enum."""

    ordering = django_filters.OrderingFilter(fields=(("price", "price"), ("name", "name")))

    class Meta:
        model = Widget
        fields = ["name"]


def _create_widget(*, name: str, price: int) -> Widget:
    """Create a widget."""
    return Widget(name=name, price=price)


def _widget(*, pk: int) -> Widget:
    """Fetch one widget by id."""
    return Widget(pk=pk, name="w")


def _widgets(**_: Any) -> Any:
    """Search widgets."""
    return Widget.objects.none()


def _vendor(*, project_pk: str, draw_request_pk: str = "", note: str = "") -> dict[str, str]:
    return {"name": project_pk}


def _toolset() -> SpecToolset:
    registry = SpecRegistry()
    # A service: a form's required fields and its bounds. Each tool but the
    # last is described by its callable's docstring, the last by
    # ``descriptions=``.
    registry.register(
        "create_widget",
        ServiceSpec(
            service=_create_widget,
            validator=widget_input(),
            presenter=widget_presenter(),
            permissions=allow(),
        ),
    )
    # A retrieve: a required read, and an ``id`` marked as a handle.
    registry.register(
        "get_widget",
        SelectorSpec(
            kind=SelectorKind.RETRIEVE,
            selector=_widget,
            reads=Parameters.of(Parameter("pk", "integer", required=True)),
            presenter=RowPresenter(
                OutputField("id", "integer", marking=FieldMarking.handle("Widget handle.")),
                OutputField("name", "string", marking=FieldMarking.label()),
                OutputField("price", "integer"),
            ),
            permissions=allow(),
        ),
    )
    # A list: ``page`` and ``limit`` under a bound, a filter set's ``ordering``
    # and filter, and a read-shaping ``fields``.
    registry.register(
        "search_widgets",
        SelectorSpec(
            kind=SelectorKind.LIST,
            selector=_widgets,
            filter_set=_SortedWidgets,
            shapes=Parameters.of(Parameter("fields", "string", help="Fields to return.")),
            presenter=widget_presenter(),
            permissions=allow(),
        ),
    )
    # Server arguments: ``project_pk`` bound to a value, ``draw_request_pk``
    # withheld, so only the optional ``note`` is left for the model.
    registry.register(
        "vendor",
        SelectorSpec(
            kind=SelectorKind.RETRIEVE,
            selector=_vendor,
            reads=Parameters.of(
                Parameter("project_pk", "string", required=True),
                Parameter("draw_request_pk", "string"),
                Parameter("note", "string", help="A note to match."),
            ),
            presenter=RowPresenter(OutputField("name", "string")),
            permissions=allow(),
        ),
    )
    return SpecToolset(
        registry,
        max_page_size=50,
        descriptions={"vendor": "The bound project's vendor."},
        tool_server_arguments={
            "vendor": ServerArguments(
                values={"project_pk": "7"}, withheld=frozenset({"draw_request_pk"})
            )
        },
    )


async def _definitions() -> dict[str, Any]:
    """Every tool ``get_tools`` offers, in its order, as the fields above."""
    tools = await _toolset().get_tools(run_context())
    return {
        "tools": [
            {field: getattr(tool.tool_def, field) for field in FIELDS} for tool in tools.values()
        ]
    }


def _encode(definitions: dict[str, Any]) -> str:
    """The dump's text: sorted keys, two-space indent, one trailing newline."""
    return json.dumps(definitions, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


async def test_the_advertised_tool_definitions_match_the_checked_in_dump():
    current = _encode(await _definitions())
    if WRITE:
        DUMP.write_text(current, encoding="utf-8")
        return
    checked_in = DUMP.read_text(encoding="utf-8") if DUMP.exists() else ""
    if current != checked_in:
        diff = "".join(
            difflib.unified_diff(
                checked_in.splitlines(keepends=True),
                current.splitlines(keepends=True),
                fromfile="tests/spec_tools/tool_schemas.json (checked in)",
                tofile="get_tools (now)",
            )
        )
        lines = diff.splitlines(keepends=True)
        shown = "".join(lines[:200]) + (
            f"... {len(lines) - 200} more lines\n" if len(lines) > 200 else ""
        )
        raise AssertionError(
            "The advertised tool definitions changed. If the change is intended, run "
            "`make tool-schemas` and commit tests/spec_tools/tool_schemas.json in the "
            f"same pull request.\n{shown}"
        )
