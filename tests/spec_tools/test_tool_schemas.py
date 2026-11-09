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
``return_schema``, ``metadata``), plus any other field set away from
pydantic-ai's default: a new defaulted pydantic-ai field does not move the
dump, and a field the toolset starts setting does.

The toolset below is built for the dump, over the spec test app's shapes: one
tool per schema feature a definition carries, not one per test spec. Adding a
feature to the definitions earns a tool here. It covers a service with a form's
bounds, a retrieve, a list with a filter set and a read-shaping argument, a
parameter with a default, a bound and a withheld server argument, a ``many``
service, a spec with no presenter, an output field hidden from the audience and
a Validator field declared only to the agent transport.
It is one toolset: a feature that needs a toolset-level setting other than
``max_page_size``, ``descriptions`` and ``tool_server_arguments`` (an omitted
tool, a pool seed, ``unknown_arguments``, conventions) is not in the dump.

A schema change regenerates the dump in the same pull request::

    make tool-schemas

which runs this test with ``TOOL_SCHEMAS_WRITE=1`` set, so it writes the file
instead of comparing, then skips so a run that wrote never reports a pass; the
diff of ``tool_schemas.json`` is then the change a reviewer reads.
"""

from __future__ import annotations

import dataclasses
import difflib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import django_filters
import pytest
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
    ValidationContext,
    Validator,
)
from pydantic_ai.tools import ToolDefinition

from django_spec_agents.spec_tools import AGENT_TRANSPORT, SpecToolset
from tests.spec_tools.kernel_specs import RowPresenter, allow, widget_input, widget_presenter
from tests.spec_tools.testapp.models import Widget
from tests.spec_tools.wording_specs import run_context

DUMP = Path(__file__).parent / "tool_schemas.json"
WRITE = os.environ.get("TOOL_SCHEMAS_WRITE") == "1"
# Always dumped: what this package sets on a definition.
FIELDS = ("name", "description", "parameters_json_schema", "return_schema", "metadata")


class _SortedWidgets(django_filters.FilterSet):
    """An ``ordering`` the kernel reads into the spec's reads, as its labelled choices."""

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


def _create_widgets(*, items: list[dict[str, Any]]) -> list[Widget]:
    """Create several widgets in one call."""
    return [Widget(**item) for item in items]


def _ping() -> dict[str, str]:
    """Check the service is up."""
    return {"status": "ok"}


class _FieldToTheAgent(Validator):
    """Declares ``title``, and a required ``channel`` to the agent transport alone."""

    def parameters(self, *, call_context: dict[str, Any] | None = None) -> Parameters:
        title = Parameter("title", "string", required=True)
        if call_context is not None and call_context["transport"] == AGENT_TRANSPORT:
            return Parameters.of(title, Parameter("channel", "string", required=True))
        return Parameters.of(title)

    def validate(self, arguments: Mapping[str, Any], context: ValidationContext) -> dict[str, Any]:
        return dict(arguments)


def _notify(*, data: dict[str, Any]) -> dict[str, Any]:
    """Send a notification."""
    return data


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
                # Hidden from the audience, so it leaves the return schema.
                OutputField("cost", "integer", marking=FieldMarking.hidden()),
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
    # withheld, so only the optional ``note`` is left for the model, which
    # declares a default, so the schema pins a ``default``.
    registry.register(
        "vendor",
        SelectorSpec(
            kind=SelectorKind.RETRIEVE,
            selector=_vendor,
            reads=Parameters.of(
                Parameter("project_pk", "string", required=True),
                Parameter("draw_request_pk", "string"),
                Parameter("note", "string", help="A note to match.", default=""),
            ),
            presenter=RowPresenter(OutputField("name", "string")),
            permissions=allow(),
        ),
    )
    # A ``many`` service: its list travels as one required array argument.
    registry.register(
        "create_widgets",
        ServiceSpec(
            service=_create_widgets,
            many=True,
            validator=widget_input(),
            presenter=widget_presenter(),
            permissions=allow(),
        ),
    )
    # No presenter: the spec declares no output, so ``return_schema`` is null.
    registry.register(
        "ping", SelectorSpec(kind=SelectorKind.RETRIEVE, selector=_ping, permissions=allow())
    )
    # A Validator field declared only to this transport: offered and required,
    # since the schema is built under the agent's stamp.
    registry.register(
        "notify",
        ServiceSpec(service=_notify, validator=_FieldToTheAgent(), permissions=allow()),
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


def _declared_default(field: dataclasses.Field[Any]) -> Any:
    """What a ``ToolDefinition`` field is when nobody sets it.

    A ``default_factory`` field has no ``default`` (it is ``MISSING``), so the
    factory is called: comparing against ``MISSING`` would dump every one.
    """
    if field.default is not dataclasses.MISSING:
        return field.default
    return field.default_factory()


def _projection(tool_def: ToolDefinition) -> dict[str, Any]:
    """The five fields always, and any other field set away from its default."""
    kept = {field: getattr(tool_def, field) for field in FIELDS}
    for field in dataclasses.fields(tool_def):
        if field.name not in kept and getattr(tool_def, field.name) != _declared_default(field):
            kept[field.name] = getattr(tool_def, field.name)
    return kept


async def _definitions() -> dict[str, Any]:
    """Every tool ``get_tools`` offers, in its order, as the fields above."""
    tools = await _toolset().get_tools(run_context())
    return {"tools": [_projection(tool.tool_def) for tool in tools.values()]}


def _encode(definitions: dict[str, Any]) -> str:
    """The dump's text: sorted keys, two-space indent, one trailing newline."""
    return json.dumps(definitions, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


async def test_the_advertised_tool_definitions_match_the_checked_in_dump():
    current = _encode(await _definitions())
    if WRITE:
        DUMP.write_text(current, encoding="utf-8")
        # A skip, not a pass: a write left exported must not turn drift into a
        # silent rewrite that CI reports green.
        pytest.skip("wrote tests/spec_tools/tool_schemas.json")
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
