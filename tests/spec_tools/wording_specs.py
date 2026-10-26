"""The specs the two wording suites run against, defined once.

``test_default_wording.py`` pins what each default says and
``test_spec_toolset_conventions.py`` changes it one field at a time. Both need
the same selectors, presenters and run context, so a line of wording lands on
the same tool in each.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import django_filters
from django_service_specs import (
    FieldMarking,
    OutputField,
    Parameter,
    Parameters,
    SelectorKind,
    SelectorSpec,
    ServiceValidationError,
)
from pydantic_ai.usage import RunUsage

from django_spec_agents import AgentDeps
from tests.spec_tools.kernel_specs import RowPresenter, allow
from tests.spec_tools.testapp.models import Widget


def run_context() -> Any:
    """The fields of a live ``RunContext`` the toolset reads."""
    return SimpleNamespace(
        deps=AgentDeps(user=None),
        run_id="run-1",
        conversation_id="conv-1",
        run_step=1,
        tool_call_id="call-1",
        usage=RunUsage(),
    )


class SortedWidgets(django_filters.FilterSet):
    ordering = django_filters.OrderingFilter(fields=(("name", "name"),))

    class Meta:
        model = Widget
        fields = []


def handled_widget() -> RowPresenter:
    """An ``id`` marked as a handle with no wording of its own, so the fallback answers."""
    return RowPresenter(
        OutputField("id", "integer", marking=FieldMarking.handle()),
        OutputField("name", "string"),
    )


def _refuse_any_selection(shapes: Mapping[str, Any]) -> None:
    if shapes.get("fields"):
        raise ServiceValidationError("Unknown field `items`.")


def picky() -> RowPresenter:
    """A row that refuses any ``fields`` selection, in its own words."""
    return RowPresenter(OutputField("name", "string"), refuse=_refuse_any_selection)


def rows() -> list[dict[str, str]]:
    """Two plain rows, so the render runs without a database.

    It takes no argument, so a ``fields`` the tool reads is a shape, which
    reaches the presenter.
    """
    return [{"name": "a"}, {"name": "b"}]


def widgets(**_: Any) -> Any:
    """A queryset, which a ``filter_set`` requires; never evaluated here."""
    return Widget.objects.none()


def one(*, pk: int) -> Widget:
    """A lookup with no default for ``pk``."""
    return Widget(pk=pk, name="w")


def approve() -> dict[str, bool]:
    """Approve the pending invoices.

    Takes no arguments, so a ``fields`` the spec reads is one of its shapes
    rather than this service's argument.
    """
    return {"approved": True}


def selector_specs() -> dict[str, Any]:
    """The three selectors that turn on the pagination, ordering and handle lines."""
    return {
        "list_widgets": SelectorSpec(
            kind=SelectorKind.LIST,
            selector=rows,
            presenter=picky(),
            permissions=allow(),
        ),
        "sorted_widgets": SelectorSpec(
            kind=SelectorKind.LIST,
            selector=widgets,
            presenter=picky(),
            filter_set=SortedWidgets,
            permissions=allow(),
        ),
        "get_widget": SelectorSpec(
            kind=SelectorKind.RETRIEVE,
            selector=one,
            reads=Parameters.of(Parameter("pk", "integer", required=True)),
            presenter=handled_widget(),
            permissions=allow(),
        ),
    }
